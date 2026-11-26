const encoder = new TextEncoder();
const MiB = 1024 * 1024;
const maxBody = 16 * MiB;
const maxBlob = 64 * MiB;
const prepared = new WeakSet();
const journal = "_reclamos_commits";
const headSql = `SELECT COALESCE(MAX(revision),0) AS revision FROM ${journal}`;
const initSql = `CREATE TABLE IF NOT EXISTS ${journal} (
  revision INTEGER PRIMARY KEY CHECK (revision > 0),
  request_id TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  changes TEXT NOT NULL
)`;

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function fail(status, message) {
  throw new HttpError(status, message);
}

function json(data, status = 200) {
  return Response.json(data, { status, headers: {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
  }});
}

async function allowed(request, secret) {
  if (typeof secret !== "string" || !secret.length) return false;
  const match = /^Bearer ([^\s]+)$/i.exec(request.headers.get("Authorization") || "");
  if (!match || match[1].length > 4096) return false;
  const [a,b] = await Promise.all([match[1],secret].map(
    (value) => crypto.subtle.digest("SHA-256",encoder.encode(value))
  ));
  return crypto.subtle.timingSafeEqual(a,b);
}

function bytesToHex(data) {
  return Array.from(new Uint8Array(data), b => b.toString(16).padStart(2,"0")).join("");
}

function declaredLength(request, max) {
  const value = request.headers.get("Content-Length");
  if (value === null) return null;
  if (!/^\d+$/.test(value)) fail(400,"Longitud inválida.");
  if (Number(value) > max) fail(413,"La solicitud supera el tamaño permitido.");
  return Number(value);
}

async function readBounded(request, max) {
  declaredLength(request,max);
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const {value,done} = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      fail(413,"La solicitud supera el tamaño permitido.");
    }
    chunks.push(value);
  }
  const result = new Uint8Array(total);
  let start = 0;
  for (const chunk of chunks) {
    result.set(chunk,start);
    start += chunk.byteLength;
  }
  return result;
}

async function readJson(request) {
  const data = await readBounded(request,maxBody);
  try {
    const value = JSON.parse(new TextDecoder("utf-8",{fatal:true}).decode(data));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value;
  } catch {
    fail(400,"Se requiere un objeto JSON válido.");
  }
}

function safeRevision(n) {
  return Number.isSafeInteger(n) && n >= 0 && n < Number.MAX_SAFE_INTEGER;
}

function validateSql(input) {
  if (typeof input !== "string" || encoder.encode(input).length > 100000) {
    fail(400,"Sentencia ausente o demasiado extensa.");
  }
  const sql = input.trim().replace(/;\s*$/, "");
  // The trusted server generates individual SQL statements. Journal tables belong to this Worker.
  if (!sql || /;|\/\*|--|\b_reclamos_commits\b/i.test(sql)) {
    fail(400,"Sentencia no admitida.");
  }
  return sql;
}

function createSql(input) {
  const sql = validateSql(input);
  if (/^INSERT\s+OR\s+IGNORE\s+INTO\s+(?:case_report_files|rs_settings)\b/i.test(sql)) return sql;
  const prefix = /^CREATE\s+(TABLE|(?:UNIQUE\s+)?INDEX|VIEW)\s+/i;
  if (!prefix.test(sql)) fail(400,"Se esperaba una definición de tabla, índice o vista.");
  // sqlite_master omits IF NOT EXISTS even when migrations originally used it.
  return sql.replace(prefix, (start) =>
    /^IF\s+NOT\s+EXISTS\s+/i.test(sql.slice(start.length)) ? start : start + "IF NOT EXISTS ");
}

function parseStatement(stmt) {
  if (!stmt || typeof stmt !== "object") fail(400,"Operación de escritura inválida.");
  const sql = validateSql(stmt.sql);
  if (!/^(?:INSERT\b|REPLACE\b|UPDATE\b|DELETE\b)/i.test(sql)) {
    fail(400,"Operación de escritura no admitida.");
  }
  if (!Array.isArray(stmt.params) || stmt.params.length>100) fail(400,"Número de parámetros inválido.");
  for (const value of stmt.params) {
    if (value === null || Number.isFinite(value)) continue;
    if (typeof value !== "string") fail(400,"Se requiere texto, número o nulo en cada parámetro.");
    if (encoder.encode(value).byteLength > 1.75*MiB) fail(413,"Un parámetro supera el tamaño permitido.");
  }
  return { sql, params:stmt.params };
}

async function ensureJournal(binding, session) {
  if (!prepared.has(binding)) {
    await session.prepare(initSql).run();
    prepared.add(binding);
  }
}

async function snapshot(request, session) {
  const {after,limit=50} = await readJson(request);
  if (!safeRevision(after) || !Number.isSafeInteger(limit) || limit<1 || limit>100) {
    fail(400,"Revisión o tamaño de página inválidos.");
  }
  const [head,page] = await session.batch([
    session.prepare(headSql),
    session.prepare(`SELECT revision,changes FROM ${journal} WHERE revision>? ORDER BY revision LIMIT ?`)
      .bind(after,limit),
  ]);
  const {revision} = head.results[0];
  if (after>revision) fail(409,"La revisión solicitada supera la disponible.");
  const entries = page.results;
  return json({revision,entries,more:(entries.at(-1)?.revision ?? after)<revision});
}

async function applySchema(request, session) {
  const {sql} = await readJson(request);
  if (!Array.isArray(sql) || sql.length>999) fail(400,"Se requiere una lista de definiciones.");
  const statements = sql.map(item => session.prepare(createSql(item)));
  if (statements.length) await session.batch(statements);
  return json({ok:true});
}

async function commit(request, session) {
  const {id,base,changes,statements} = await readJson(request);
  if (!safeRevision(base) || typeof id!=="string" || !/^[a-zA-Z0-9._:-]{1,128}$/.test(id)
      || typeof changes!=="string" || encoder.encode(changes).length>65536
      || !Array.isArray(statements) || statements.length>998) {
    fail(400,"Identidad, revisión o contenido de la transacción inválidos.");
  }
  const operations = statements.map(parseStatement);
  const fingerprint = bytesToHex(await crypto.subtle.digest("SHA-256",
    encoder.encode(JSON.stringify([base,changes,operations]))));
  // A failed CAS inserts revision 0 and violates the table CHECK. D1 rolls back the entire batch.
  const claim = session.prepare(`INSERT INTO ${journal}(revision,request_id,fingerprint,changes)
      VALUES(CASE WHEN (${headSql})=? THEN ? ELSE 0 END,?,?,?)`)
    .bind(base, base+1, id, fingerprint, changes);
  try {
    await session.batch([
      claim,
      session.prepare("PRAGMA defer_foreign_keys = ON"),
      ...operations.map(op => session.prepare(op.sql).bind(...op.params)),
    ]);
  } catch (error) {
    const [prior,head] = await session.batch([
      session.prepare(`SELECT revision,fingerprint FROM ${journal} WHERE request_id=?`).bind(id),
      session.prepare(headSql),
    ]);
    const saved = prior.results[0];
    if (saved && saved.fingerprint===fingerprint) return json({revision:saved.revision});
    if (saved || head.results[0].revision!==base) fail(409,"La transacción o la revisión ya cambió.");
    throw error;
  }
  return json({revision:base+1});
}

async function blob(request, files, key) {
  if (request.method === "GET") {
    const object = await files.get(key);
    if (!object) fail(404,"Objeto no encontrado.");
    return new Response(object.body, { headers: {
      "Cache-Control": "private, no-store",
      "Content-Type": "application/octet-stream",
      "Content-Length": String(object.size),
      "X-Content-Type-Options": "nosniff",
    }});
  }
  if (request.method !== "PUT") fail(405,"Método no admitido.");
  const size = declaredLength(request,maxBlob);
  // R2 validates the checksum before making an object visible. Streaming avoids buffering large PDFs.
  // Unknown-length streams are bounded separately to keep memory below the isolate limit.
  const body = size === null ? await readBounded(request,8*MiB) : request.body ?? new Uint8Array();
  try {
    await files.put(key,body,{sha256:key});
  } catch (error) {
    if (/checksum|digest|hash mismatch/i.test(String(error))) fail(422,"El contenido difiere de su SHA-256.");
    throw error;
  }
  return json({sha256:key});
}

export default {
  async fetch(request, env) {
    try {
      if (!await allowed(request,env.STORAGE_TOKEN)) return json({error:"Acceso privado requerido."},401);
      const {pathname} = new URL(request.url);
      const asset = /^\/blobs\/([a-f0-9]{64})$/.exec(pathname);
      if (asset) return await blob(request,env.FILES,asset[1]);
      const method = {"/revision":"GET","/schema":"POST","/snapshot":"POST","/commit":"POST"}[pathname];
      if (!method || typeof method !== "string") fail(404,"Ruta no encontrada.");
      if (request.method!==method) fail(405,"Método no admitido.");
      const session = env.DB.withSession("first-primary");
      await ensureJournal(env.DB,session);
      if (pathname==="/revision") return json(await session.prepare(headSql).first());
      if (pathname==="/schema") return await applySchema(request,session);
      if (pathname==="/snapshot") return await snapshot(request,session);
      return await commit(request,session);
    } catch (error) {
      if (error instanceof HttpError) return json({error:error.message},error.status);
      // D1 and R2 errors can contain SQL or customer content. Do not log or echo them.
      return json({error:"No se pudo completar la operación de almacenamiento."},503);
    }
  },
};
