#!/usr/bin/env node
/**
 * Merge verified candidate sources into src/data/sources.json.
 *
 *   node scripts/merge-sources.mjs <candidates-enriched.json>
 *
 * <candidates-enriched.json> is an array of objects with at least
 * {name, homepage, category, tier, paywalled, feed?, alive?} — typically the
 * output of `scripts/feed-health.mjs` run over scavenged candidates. Candidates
 * marked `alive:false` are dropped; the rest are deduped against the existing
 * set (by canonical URL and normalized name), tier-1 is capped at 12%, the
 * result is cleaned to the 6-field contract and written back sorted
 * (category -> tier -> name). Idempotent. See docs/sources-curation.md.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCES = resolve(__dirname, '../src/data/sources.json');
const SOURCE_CATEGORIES = ['aggregators','ai_companies','devtools','expert_blogs','hardware_science','news','newsletters','physics','research','science','security','tech_news'];
const TRACKING = /^(utm_|fbclid$|gclid$|mc_|ref$|source$|igshid$)/i;

function canon(input){const raw=(input||'').trim();try{const u=new URL(raw);const host=u.hostname.toLowerCase().replace(/^www\./,'');const path=u.pathname.replace(/\/+$/,'');const kept=[];for(const [k,v] of [...u.searchParams.entries()].sort()){if(TRACKING.test(k))continue;kept.push(`${k}=${v}`);}return `${host}${path}${kept.length?'?'+kept.join('&'):''}`;}catch{return raw.toLowerCase().replace(/^https?:\/\//,'').replace(/^www\./,'').replace(/#.*$/,'').replace(/\?.*$/,'').replace(/\/+$/,'');}}
function norm(n){return n.toLowerCase().replace(/\([^)]*\)/g,' ').replace(/[^a-z0-9]+/g,' ').trim();}
function pathLen(h){try{return new URL(h).pathname.replace(/\/+$/,'').length;}catch{return 0;}}
function rank(s){return [s.tier, s.feed?0:1, -pathLen(s.homepage), s.name.length];}
function better(a,b){const ra=rank(a),rb=rank(b);for(let i=0;i<ra.length;i++)if(ra[i]!==rb[i])return ra[i]<rb[i]?a:b;return a;}
function normFeed(f){if(!f)return null;f=f.replace(/^feed:\/\//i,'https://').replace(/^\/\//,'https://');return /^https?:\/\//i.test(f)?f:null;}
function clean(s){return {name:s.name,homepage:s.homepage,category:s.category,tier:s.tier,paywalled:!!s.paywalled,feed:normFeed(s.feed)};}

const candPath = process.argv[2];
if(!candPath){console.error('usage: node scripts/merge-sources.mjs <candidates-enriched.json>');process.exit(2);}

const existing = JSON.parse(readFileSync(SOURCES,'utf8'));
const candidates = JSON.parse(readFileSync(resolve(candPath),'utf8'));

const exUrls = new Set(existing.map(s=>canon(s.homepage)));
const exNames = new Set(existing.map(s=>norm(s.name)));
const addUrls = new Set(), addNames = new Set();
let added=[], rejDead=0, rejCat=0, rejDup=0, rejBad=0;
for(const c of candidates){
  if(!c||!c.homepage||!c.name||!/^https?:\/\//.test(c.homepage)){rejBad++;continue;}
  if(c.alive===false){rejDead++;continue;}
  if(!SOURCE_CATEGORIES.includes(c.category)){rejCat++;continue;}
  const k=canon(c.homepage), nk=norm(c.name);
  if(exUrls.has(k)||addUrls.has(k)||exNames.has(nk)||addNames.has(nk)){rejDup++;continue;}
  if(![1,2,3].includes(c.tier)) c.tier=3;
  addUrls.add(k); addNames.add(nk); added.push(c);
}

let merged=[...existing,...added];

// safety nets: collapse any residual name / url collisions, keeping the better
function collapse(merged, keyOf){
  const g=new Map();
  for(const s of merged){const k=keyOf(s);if(!g.has(k))g.set(k,[]);g.get(k).push(s);}
  const keep=new Set();let drops=0;
  for(const [,arr] of g){let w=arr[0];for(let i=1;i<arr.length;i++){w=better(w,arr[i]);drops++;}keep.add(w);}
  return {list:merged.filter(s=>keep.has(s)),drops};
}
let r1=collapse(merged, s=>norm(s.name)); merged=r1.list;
let r2=collapse(merged, s=>canon(s.homepage)); merged=r2.list;

// tier-1 cap at 12%, demoting newly-added flagship claims first
const cap=Math.floor(merged.length*0.12);
let t1=merged.filter(s=>s.tier===1);
if(t1.length>cap){
  const addedSet=new Set(added.map(s=>canon(s.homepage)));
  const demotable=merged.filter(s=>s.tier===1&&addedSet.has(canon(s.homepage))).sort((a,b)=>b.name.length-a.name.length);
  let need=t1.length-cap;
  for(const s of demotable){if(need<=0)break;s.tier=2;need--;}
}

const catIndex=(c)=>{const i=SOURCE_CATEGORIES.indexOf(c);return i===-1?99:i;};
const final=merged.map(clean).sort((a,b)=>catIndex(a.category)-catIndex(b.category)||a.tier-b.tier||a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
writeFileSync(SOURCES, JSON.stringify(final,null,2)+'\n');

const byTier={},byCat={};let withFeed=0;
for(const s of final){byTier[s.tier]=(byTier[s.tier]||0)+1;byCat[s.category]=(byCat[s.category]||0)+1;if(s.feed)withFeed++;}
console.log(`merge-sources: existing ${existing.length} + candidates ${candidates.length} -> added ${added.length}`);
console.log(`  rejected: dead ${rejDead}, badCat ${rejCat}, dup ${rejDup}, malformed ${rejBad}; collision drops name ${r1.drops} url ${r2.drops}`);
console.log(`  FINAL ${final.length} | feeds ${withFeed} (${(withFeed/final.length*100).toFixed(1)}%) | tier ${JSON.stringify(byTier)} (t1 ${(byTier[1]/final.length*100).toFixed(1)}%)`);
