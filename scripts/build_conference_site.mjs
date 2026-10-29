#!/usr/bin/env node
/** Publish repository documentation and existing conference artifacts as static HTML. */
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(root, 'tools/openspec/package.json'));
const {marked} = require('marked');
const output = path.join(root, '_build/conference-site/literate-ai');
const base = '/literate-ai/';
const repository = 'https://github.com/jordanhubbard/literate-ai';
const files = execFileSync('git', ['ls-files', '-z'], {cwd: root, encoding: 'utf8'})
  .split('\0').filter(Boolean);
const roots = ['docs/', 'labs/', 'media/', 'components/', 'flavors/', 'skills/', 'samples/', 'schemas/'];
const published = new Set(files.filter(file => roots.some(prefix => file.startsWith(prefix))
  || ['README.md', 'PROJECT.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SKILL.md', 'LICENSE'].includes(file)));
// New authored conference pages are part of this build before their first commit.
for (const name of ['README.md', 'presenter-guide.md']) published.add(`docs/conference/${name}`);
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const webPath = file => base + file.split('/').map(encodeURIComponent).join('/').replace(/\.md$/i, '.html');
function hrefFor(href, source) {
  if (href?.startsWith('https://jordanhubbard.github.io/literate-ai/')) return href.slice('https://jordanhubbard.github.io'.length);
  const github = href?.match(/^https:\/\/github\.com\/jordanhubbard\/literate-ai\/blob\/main\/([^?#]+)(.*)$/);
  if (github && published.has(github[1])) return webPath(github[1]) + github[2];
  if (!href || /^(?:[a-z][a-z0-9+.-]*:|\/|#)/i.test(href)) return href;
  const match = href.match(/^([^?#]*)(.*)$/);
  let file;
  try { file = path.posix.normalize(path.posix.join(path.posix.dirname(source), decodeURIComponent(match[1]))); }
  catch { return href; }
  if (published.has(file)) return webPath(file) + match[2];
  for (const index of ['index.html', 'README.md']) {
    const candidate = path.posix.join(file, index);
    if (published.has(candidate)) return webPath(candidate) + match[2];
  }
  return `${repository}/blob/main/${file.split('/').map(encodeURIComponent).join('/')}${match[2]}`;
}
const stylesheet = `
:root{color-scheme:light;--ink:#152b3b;--muted:#506778;--line:#d5e0e5;--paper:#fffefb;--accent:#087c79}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:#086e79;text-underline-offset:.2em}a:hover{color:#a64819}a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #e89b30;outline-offset:4px}
.skip{position:absolute;left:1rem;top:-5rem;background:white;padding:1rem;z-index:20}.skip:focus{top:1rem}
header{background:#132e3e;color:white;padding:1rem max(1rem,calc((100vw - 1160px)/2));display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none}header .brand{font-size:1.35rem;font-weight:750;margin-right:auto}header nav{display:flex;gap:1.1rem;flex-wrap:wrap;font-size:.94rem}
.search{position:relative;margin:.3rem 0}input{max-width:100%;width:220px;border:1px solid #b7c9d0;border-radius:6px;padding:.6rem .8rem;font:inherit;background:white;color:var(--ink)}
#search-results{position:absolute;right:0;top:100%;width:min(440px,85vw);background:white;color:var(--ink);box-shadow:0 8px 24px #0003;z-index:10;max-height:70vh;overflow:auto;border-radius:6px}
#search-results a{display:block;color:#086e79;padding:.65rem 1rem;border-bottom:1px solid var(--line)}#search-results p{padding:.7rem 1rem;margin:0}
main{max-width:1000px;margin:0 auto;padding:2.6rem 1.4rem 4rem}h1{font-size:clamp(2rem,5vw,3.6rem);line-height:1.12;letter-spacing:-.035em;margin:.4rem 0 1.4rem}h2{font-size:1.7rem;line-height:1.25;margin-top:2.3rem;scroll-margin-top:1rem}h3{line-height:1.35;scroll-margin-top:1rem}
p,li{overflow-wrap:anywhere}p{margin:.85rem 0}li{margin:.3rem 0}img,video,svg{max-width:100%;height:auto}video{display:block;width:100%;background:#132e3e;border-radius:10px;margin:1rem 0}
table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse;font-size:.94rem}th,td{padding:.65rem .85rem;border:1px solid var(--line);text-align:left;vertical-align:top}th{background:#edf4f5}
pre{overflow:auto;background:#edf3f5;border:1px solid var(--line);border-radius:7px;padding:1rem;font-size:.85rem;line-height:1.55}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.87em}p code,li code{background:#edf3f5;padding:.1rem .25rem;border-radius:3px}
blockquote{margin:1.4rem 0;border-left:4px solid #e89b30;padding:.1rem 1.1rem;background:#fff6e5}.eyebrow{text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-size:.8rem;font-weight:700}.page-tools{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;font-size:.88rem;color:var(--muted);padding-top:2rem;border-top:1px solid var(--line);margin-top:3rem}
.mermaid{background:transparent;border:0;text-align:center}.mermaid svg{height:auto}footer{padding:1.5rem;text-align:center;background:#edf3f5;color:var(--muted);font-size:.88rem}
@media(max-width:600px){header{padding:1rem}header nav{gap:.8rem}.search,input{width:100%}main{padding:1.7rem 1rem 3rem}h1{font-size:2.25rem}ul,ol{padding-left:1.4rem}}
@media print{header,.page-tools,footer,.search{display:none}main{max-width:none;padding:0}a{color:inherit}pre{white-space:pre-wrap}h2,h3{break-after:avoid}}
`;
const searchScript = `
const input=document.querySelector('#site-search'), results=document.querySelector('#search-results');
let index;
input.addEventListener('input',async()=>{
 const query=input.value.trim().toLowerCase();results.replaceChildren();results.hidden=!query;if(!query)return;
 try{index??=await fetch('${base}search-index.json').then(r=>{if(!r.ok)throw Error(r.status);return r.json()});
 const terms=query.split(/\\s+/);const matches=index.filter(x=>terms.every(t=>(x.title+' '+x.headings).toLowerCase().includes(t))).slice(0,12);
 for(const item of matches){const a=document.createElement('a');a.href=item.href;a.textContent=item.title;results.append(a)}
 if(!matches.length){const p=document.createElement('p');p.textContent='No matching guide. Try “Components”, “HTML”, “install” or “workers”.';results.append(p)}
 }catch{const p=document.createElement('p');p.textContent='Search is unavailable. Open the documentation index from the navigation.';results.append(p)}
});
input.addEventListener('keydown',e=>{if(e.key==='Escape'){results.hidden=true;input.value=''}});
document.addEventListener('click',e=>{if(!e.target.closest('.search'))results.hidden=true});
if(document.querySelector('.mermaid')){const script=document.createElement('script');script.src='${base}assets/mermaid.min.js';script.onload=()=>mermaid.initialize({startOnLoad:true,securityLevel:'strict',theme:'neutral'});document.head.append(script)}
`;
const search = [];
function render(source, markdown, target) {
  const renderer = new marked.Renderer();
  renderer.link = (href, title, text) => `<a href="${escape(hrefFor(href, source))}"${title ? ` title="${escape(title)}"` : ''}>${text}</a>`;
  renderer.image = (href, title, text) => `<img src="${escape(hrefFor(href, source))}" alt="${escape(text)}" loading="lazy"${title ? ` title="${escape(title)}"` : ''}>`;
  const originalCode = renderer.code.bind(renderer);
  renderer.code = (code, lang, escaped) => lang === 'mermaid' ? `<pre class="mermaid">${escape(code)}</pre>` : originalCode(code, lang, escaped);
  const title = markdown.match(/^#\s+(.+)$/m)?.[1].replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') || path.basename(source);
  let content = marked.parse(markdown, {renderer, gfm:true, mangle:false, headerIds:true});
  content = content.replace(/\b(href|src|poster)=(["'])(.*?)\2/g, (_, name, quote, href) => `${name}=${quote}${escape(hrefFor(href.replaceAll('&amp;', '&'), source))}${quote}`);
  content = content.replace('<!-- CONFERENCE_VIDEO -->', `<video controls preload="metadata" poster="${base}media/courses/eight-hours-to-make-coffee/poster.png" aria-label="Eight Hours to Make Coffee, animated onboarding film"><source src="${base}media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.mp4" type="video/mp4"><track default kind="captions" srclang="en" label="English" src="${base}media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.vtt">Your browser cannot play this video. Use the MP4 download below.</video>`);
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Literate AI conference materials and documentation: readable specifications, Components, evidence, and practical examples."><link rel="icon" href="data:,"><title>${escape(title)} · Literate AI</title><link rel="stylesheet" href="${base}assets/site.css"></head><body><a class="skip" href="#content">Skip to content</a><header><a class="brand" href="${base}">Literate AI</a><nav aria-label="Primary"><a href="${base}docs/README.html">Documentation</a><a href="${base}docs/presentations/literate-ai-evolution-conference/index.html">Slides</a><a href="${base}labs/README.html">Labs</a><a href="${repository}">GitHub</a></nav><div class="search"><label for="site-search" hidden>Search documentation</label><input id="site-search" type="search" placeholder="Find a guide…" aria-label="Search documentation" autocomplete="off"><div id="search-results" hidden></div></div></header><main id="content"><p class="eyebrow">${source.startsWith('docs/conference/') ? 'Conference materials' : 'Repository documentation'}</p>${content}<div class="page-tools"><a href="${repository}/blob/main/${source}">Read the Markdown source</a><a href="${base}">Conference home</a><span>Published 1.3.1 · Development 1.4</span></div></main><footer>Readable intent. Reviewable plans. Evidence tied to the work. <a href="${repository}/releases/latest">Latest published release</a></footer><script src="${base}assets/site.js" defer></script></body></html>`;
  fs.mkdirSync(path.dirname(target), {recursive:true});
  fs.writeFileSync(target, html);
  search.push({title,href:webPath(source),headings:[...markdown.matchAll(/^#{1,4}\s+(.+)$/gm)].map(x=>x[1]).join(' ')});
}
fs.mkdirSync(path.join(output, 'assets'), {recursive:true});
fs.writeFileSync(path.join(output, 'assets/site.css'), stylesheet);
fs.writeFileSync(path.join(output, 'assets/site.js'), searchScript);
fs.copyFileSync(require.resolve('mermaid/dist/mermaid.min.js'), path.join(output, 'assets/mermaid.min.js'));
let count=0;
for (const file of published) {
  const source=path.join(root,file);
  if (!fs.existsSync(source) || !fs.statSync(source).isFile()) continue;
  const target=path.join(output,file.replace(/\.md$/i,'.html'));
  if (/\.md$/i.test(file)) render(file,fs.readFileSync(source,'utf8'),target);
  else {fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(source,target);}
  count++;
}
render('docs/conference/README.md',fs.readFileSync(path.join(root,'docs/conference/README.md'),'utf8'),path.join(output,'index.html'));
fs.writeFileSync(path.join(output,'search-index.json'),JSON.stringify(search));
fs.writeFileSync(path.join(output,'.nojekyll'),'');
const revision=execFileSync('git',['rev-parse','HEAD'],{cwd:root,encoding:'utf8'}).trim();
fs.writeFileSync(path.join(output,'publication.json'),JSON.stringify({revision,files:count,markdown_pages:search.length,release_status:'1.3.1 published; Windows matrix temporarily suspended due to resource availability and restored next release; 1.4 development'},null,2)+'\n');
const bundle=path.join(root,'_build/conference-download');
fs.rmSync(bundle,{recursive:true,force:true});
fs.mkdirSync(bundle,{recursive:true});
const artifacts = {
  'slides.html':'docs/presentations/literate-ai-evolution-conference/index.html',
  'slides.pptx':'docs/presentations/literate-ai-evolution-conference/literate-ai-evolution-conference.pptx',
  'onboarding.mp4':'media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.mp4',
  'onboarding.vtt':'media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.vtt',
  'onboarding.srt':'media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.srt',
};
for(const file of published)if(file.startsWith('labs/'))artifacts[file]=file;
for(const [name,file] of Object.entries(artifacts)){const target=path.join(bundle,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(path.join(root,file),target);}
fs.writeFileSync(path.join(bundle,'START-HERE.txt'),'Literate AI conference materials\n\nWelcome aboard, Unprompted AI! Open slides.html in any modern browser. Arrow keys navigate; N opens speaker notes; browser Print produces a handout. Each labs/*/index.html is self-contained and opens offline. Complete lab projects and initial retained inputs are included: read labs/README.md, install Literate AI 1.3.1, then use litai build and litai run from a lab project directory. Progressive multi-Component curriculum work remains open. Play onboarding.mp4 with the matching captions.\n\nWebsite: https://jordanhubbard.github.io/literate-ai/\nSource and current release status: https://github.com/jordanhubbard/literate-ai\n1.3.1 is published; 1.4 is development. See the release notes for qualification scope.\n');
fs.mkdirSync(path.join(output,'downloads'),{recursive:true});
execFileSync('python3',['-m','zipfile','-c',path.join(output,'downloads/literate-ai-conference-kit.zip'),'START-HERE.txt',...Object.keys(artifacts).filter(name=>!name.startsWith('labs/')),'labs'],{cwd:bundle});
console.log(JSON.stringify({output,files:count,markdown_pages:search.length,revision}));
