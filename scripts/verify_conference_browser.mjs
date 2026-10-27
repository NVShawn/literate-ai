#!/usr/bin/env node
/** Scoped browser checks for attendee navigation, search, media and mobile layout. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath, pathToFileURL} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'tools/openspec/package.json'));
const {default:puppeteer}=await import(pathToFileURL(require.resolve('puppeteer')));
const base=(process.argv[2]||'http://127.0.0.1:8766/literate-ai/').replace(/\/?$/,'/');
const output=path.join(root,'_build/conference-website-browser',base.startsWith('https:')?'live':'local');
await fs.mkdir(output,{recursive:true});
const browser=await puppeteer.launch({headless:true,executablePath:process.env.CONFERENCE_CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const results=[];
try{
  for(const width of [1440,390]){
    const page=await browser.newPage();
    await page.setCacheEnabled(false);
    await page.setViewport({width,height:1000,deviceScaleFactor:1});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('console',message=>{if(message.type()==='error')errors.push(message.text())});
    page.on('response',response=>{if(response.status()>=400)errors.push(`${response.status()} ${response.url()}`)});
    for(const suffix of ['', 'docs/README.html','docs/user/getting-started.html','docs/user/concepts.html','docs/user/html-repository-explorer.html','docs/conference/presenter-guide.html']){
      const response=await page.goto(base+suffix,{waitUntil:'networkidle0'});
      assert.equal(response.status(),200,`${width} ${suffix}`);
      assert.ok(await page.$('main h1'),`missing heading ${suffix}`);
      assert.ok(await page.$('nav[aria-label="Primary"]'),`missing navigation ${suffix}`);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`overflow ${width} ${suffix}`);
      if(!suffix){
        await page.type('#site-search','Components');
        await page.waitForSelector('#search-results a',{visible:true});
        assert.ok(await page.$$eval('#search-results a',links=>links.length)>0);
        await page.keyboard.press('Escape');
        assert.ok(await page.$eval('#search-results',node=>node.hidden));
        assert.ok(await page.$('video track[kind="captions"]'));
        await page.waitForFunction(()=>Number.isFinite(document.querySelector('video').duration));
        assert.ok(Math.abs(await page.$eval('video',video=>video.duration)-724.896322)<1);
        await page.screenshot({path:path.join(output,`home-${width}.png`),fullPage:true});
        await fs.writeFile(path.join(output,`home-${width}-structure.txt`),await page.$eval('main',node=>node.innerText));
      }
      results.push({width,path:suffix||'index.html',status:200,overflow:false});
    }
    await page.goto(base+'labs/00-grumpy-counter/index.html',{waitUntil:'networkidle0'});
    const previous=await page.$eval('input[type=range]',node=>node.value);
    await page.focus('input[type=range]');
    await page.keyboard.press('ArrowRight');
    assert.notEqual(await page.$eval('input[type=range]',node=>node.value),previous);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    results.push({width,path:'labs/00-grumpy-counter/index.html',interaction:'modulus changes'});
    assert.deepEqual(errors,[],`${width} browser errors`);
    await page.close();
  }
  console.log(JSON.stringify({base,results,findings:[]},null,2));
  await fs.writeFile(path.join(output,'results.json'),JSON.stringify({base,results,findings:[]},null,2)+'\n');
}finally{await browser.close()}
