import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile,readdir,lstat} from 'node:fs/promises';
import {resolve,join} from 'node:path';

const root=resolve(process.cwd());
const isPlain=(x)=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const hex64=/^[0-9a-f]{64}$/;
const assetName=/^assets\/index-[A-Za-z0-9_-]+\.(?:js|css)$/;

async function allFiles(dir,prefix=''){
 const out=[];
 for(const e of await readdir(dir,{withFileTypes:true})){
  assert.ok(!e.isSymbolicLink(),'Build symlink forbidden');
  const name=prefix?prefix+'/'+e.name:e.name;
  if(e.isDirectory())out.push(...await allFiles(join(dir,e.name),name));
  else {assert.ok(e.isFile(),'Non-regular build entry');out.push(name);}
 }
 return out.sort();
}

test('CI-built frontend files must exactly match read-only v35 candidate manifest',async()=>{
 const manifest=JSON.parse(await readFile(join(root,'v35_1_dist_integrity.json'),'utf8'));
 assert.equal(manifest.schema,'v34_funnest_build_integrity_v1');
 assert.equal(manifest.live_trading_enabled,false);
 assert.equal(manifest.receiver_time_only,true);
 assert.match(manifest.git_commit,/^[0-9a-f]{40}$/);
 assert.ok(isPlain(manifest.files));
 const names=Object.keys(manifest.files).sort();
 assert.equal(names.length,3);
 assert.ok(names.includes('index.html'));
 assert.equal(names.filter(x=>assetName.test(x)&&x.endsWith('.js')).length,1);
 assert.equal(names.filter(x=>assetName.test(x)&&x.endsWith('.css')).length,1);
 assert.deepEqual(await allFiles(join(root,'dist_v35_1')),names);
 for(const name of names){
  assert.equal(name.startsWith('/')||name.includes('..'),false);
  const descriptor=manifest.files[name];
  assert.ok(isPlain(descriptor));
  assert.deepEqual(Object.keys(descriptor).sort(),['bytes','sha256']);
  assert.match(descriptor.sha256,hex64);
  assert.ok(Number.isSafeInteger(descriptor.bytes)&&descriptor.bytes>0);
  const file=join(root,'dist_v35_1',name);
  assert.ok((await lstat(file)).isFile());
  const bytes=await readFile(file);
  assert.equal(bytes.length,descriptor.bytes,name);
  assert.equal(createHash('sha256').update(bytes).digest('hex'),descriptor.sha256,name);
 }
 const html=await readFile(join(root,'dist_v35_1','index.html'),'utf8');
 const refs=[...html.matchAll(/\/assets\/index-[A-Za-z0-9_-]+\.(?:js|css)/g)].map(x=>x[0].slice(1)).sort();
 assert.deepEqual(refs,names.filter(x=>x!=='index.html'));
});
