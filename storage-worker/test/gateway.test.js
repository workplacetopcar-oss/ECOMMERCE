import assert from "node:assert/strict";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const path = fileURLToPath(new URL("../src/worker.js",import.meta.url));
const token = randomBytes(32).toString("hex");
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
let mf;
const url = "http://localhost";
function query(path, method="GET", body, auth=token, headers={}) {
  return mf.dispatchFetch(url+path,{
    method, headers:{Authorization:"Bearer "+auth,...headers},
    body:body===undefined?undefined:JSON.stringify(body),
  });
}
function write(base,key) {
  return {base,id:randomUUID(),changes:sha(key),statements:[
    {sql:"INSERT INTO cases(id) VALUES(?)",params:[key]}
  ]};
}

before(async()=>{
  mf = new Miniflare(convertV4MiniflareOptions({
    cf:false, modules:true, scriptPath:path, compatibilityDate:"2026-08-01",
    d1Databases:["DB"],r2Buckets:["FILES"],bindings:{STORAGE_TOKEN:token},
  }));
  await mf.ready;
});
after(async()=>{ if(mf) await mf.dispose(); });

test("todas las rutas requieren el secreto",async()=>{
  for(const [route,method] of [["/revision","GET"],["/schema","POST"],["/commit","POST"],["/snapshot","POST"],["/blobs/"+sha("x"),"GET"],["/blobs/"+sha("x"),"PUT"]]) {
    assert.equal((await query(route,method,undefined,"incorrecto")).status,401);
  }
  assert.deepEqual(await (await query("/revision")).json(),{revision:0});
});

test("esquema repetible y transacción completa o revertida",async()=>{
  const schema={sql:["CREATE TABLE cases (id TEXT PRIMARY KEY)", "CREATE TABLE IF NOT EXISTS rs_settings (id INTEGER PRIMARY KEY)", "INSERT OR IGNORE INTO rs_settings VALUES(1)"]};
  for(let i=0;i<2;i++) assert.equal((await query("/schema","POST",schema)).status,200);
  const brokenSchema=await query("/schema","POST",{sql:["CREATE TABLE provisional(id INT)","CREATE TABLE broken("]});
  assert.equal(brokenSchema.status,503);
  const db=await mf.getD1Database("DB");
  assert.equal(await db.prepare("SELECT name FROM sqlite_master WHERE name='provisional'").first(),null);
  const first=write(0,"uno");
  assert.deepEqual(await (await query("/commit","POST",first)).json(),{revision:1});
  assert.deepEqual(await (await query("/commit","POST",first)).json(),{revision:1});
  assert.equal((await query("/commit","POST",{...first,changes:"distinto"})).status,409);
  const invalid=write(1,"parcial");
  invalid.statements.push({sql:"INSERT INTO inexistente VALUES (?)",params:[1]});
  assert.equal((await query("/commit","POST",invalid)).status,503);
  assert.equal(await db.prepare("SELECT id FROM cases WHERE id='parcial'").first(),null);
  assert.deepEqual(await (await query("/revision")).json(),{revision:1});
});

test("dos escritores al mismo tiempo, reintento y paginación coherente",async()=>{
  const a=write(1,"dos"), b=write(1,"tres");
  const responses=await Promise.all([a,b].map(input=>query("/commit","POST",input)));
  assert.deepEqual(responses.map(r=>r.status).sort(),[200,409]);
  const winner=responses[0].status===200?a:b;
  assert.deepEqual(await (await query("/commit","POST",winner)).json(),{revision:2});
  const first=await (await query("/snapshot","POST",{after:0,limit:1})).json();
  assert.equal(first.revision,2);
  assert.equal(first.more,true);
  assert.deepEqual(first.entries,[{revision:1,changes:sha("uno")}]);
  assert.deepEqual(await (await query("/snapshot","POST",{after:1,limit:1})).json(),
    {revision:2,entries:[{revision:2,changes:winner.changes}],more:false});
  assert.equal((await query("/snapshot","POST",{after:3})).status,409);
});

test("R2 verifica SHA-256, conserva el original y sirve bytes privados",async()=>{
  const body=randomBytes(8192), key=sha(body), path="/blobs/"+key;
  const put=(key,data)=>mf.dispatchFetch(url+"/blobs/"+key,{method:"PUT",headers:{Authorization:"Bearer "+token},body:data});
  const bad=await put(key,Buffer.from("corrupto"));
  assert.equal(bad.status,422);
  assert.equal((await query(path)).status,404);
  assert.equal((await put(key,body)).status,200);
  assert.equal((await put(key,body)).status,200);
  assert.equal((await put(key,Buffer.from("corrupto"))).status,422);
  const res=await query(path);
  assert.equal(res.headers.get("Cache-Control"),"private, no-store");
  assert.deepEqual(Buffer.from(await res.arrayBuffer()),body);
  const zero=await put(sha(""),Buffer.alloc(0));
  assert.equal(zero.status,200);
});

test("validación de tipos, límites y tabla reservada",async()=>{
  assert.equal((await query("/schema","POST",{sql:["CREATE TABLE t(x); DELETE FROM cases"]})).status,400);
  assert.equal((await query("/commit","POST",{...write(2,"bad"),statements:[{sql:"DELETE FROM _reclamos_commits",params:[]}]})).status,400);
  assert.equal((await query("/snapshot","POST",{after:-1})).status,400);
  assert.equal((await query("/snapshot","POST",{after:0,limit:101})).status,400);
  const large={sql:"INSERT INTO cases VALUES(?)",params:["x".repeat(2*1024*1024)]};
  assert.equal((await query("/commit","POST",{...write(2,"grande"),statements:[large]})).status,413);
});
