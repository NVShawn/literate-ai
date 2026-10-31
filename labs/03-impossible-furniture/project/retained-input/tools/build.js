const fs=require('node:fs'),path=require('node:path');const out=path.resolve(process.argv[2]);fs.mkdirSync(path.dirname(out),{recursive:true});fs.copyFileSync('main.js',out);
