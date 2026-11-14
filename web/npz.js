// Read a numpy .npz (as written by np.savez_compressed) into typed arrays.
//
// An .npz is a zip of .npy files. Everything here is the browser's own: the
// zip directory is a few fixed-layout records, and deflate is the native
// DecompressionStream. No library, no build step.

const td = new TextDecoder();

// Inflate one raw-deflate member.
async function inflate(bytes) {
  const stream = new Blob([bytes]).stream()
    .pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

// List the zip's members from its central directory. The central directory is
// the authority for sizes: a local header may defer them to a data descriptor.
function zipEntries(buf) {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  // End-of-central-directory record: scan back for its signature (it ends
  // with a variable-length comment, so its position is not fixed).
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("not a zip file");
  let count = dv.getUint16(eocd + 10, true);
  let cdOffset = dv.getUint32(eocd + 16, true);
  // Zip64 locator sits just before the EOCD when the archive needed it.
  if (cdOffset === 0xffffffff || count === 0xffff) {
    const loc = eocd - 20;
    if (dv.getUint32(loc, true) !== 0x07064b50) throw new Error("bad zip64");
    const z64 = Number(dv.getBigUint64(loc + 8, true));
    count = Number(dv.getBigUint64(z64 + 32, true));
    cdOffset = Number(dv.getBigUint64(z64 + 48, true));
  }

  const out = [];
  let p = cdOffset;
  for (let n = 0; n < count; n++) {
    if (dv.getUint32(p, true) !== 0x02014b50) throw new Error("bad zip directory");
    const method = dv.getUint16(p + 10, true);
    let csize = dv.getUint32(p + 20, true);
    let usize = dv.getUint32(p + 24, true);
    const nameLen = dv.getUint16(p + 28, true);
    const extraLen = dv.getUint16(p + 30, true);
    const commentLen = dv.getUint16(p + 32, true);
    let local = dv.getUint32(p + 42, true);
    const name = td.decode(buf.subarray(p + 46, p + 46 + nameLen));
    // Zip64 extra field: the 0xffffffff fields appear in this fixed order.
    let e = p + 46 + nameLen;
    const eEnd = e + extraLen;
    while (e + 4 <= eEnd) {
      const id = dv.getUint16(e, true), len = dv.getUint16(e + 2, true);
      if (id === 0x0001) {
        let q = e + 4;
        if (usize === 0xffffffff) { usize = Number(dv.getBigUint64(q, true)); q += 8; }
        if (csize === 0xffffffff) { csize = Number(dv.getBigUint64(q, true)); q += 8; }
        if (local === 0xffffffff) { local = Number(dv.getBigUint64(q, true)); q += 8; }
      }
      e += 4 + len;
    }
    // The member's data follows its *local* header, whose extra field can
    // differ in length from the central one.
    const lName = dv.getUint16(local + 26, true);
    const lExtra = dv.getUint16(local + 28, true);
    const start = local + 30 + lName + lExtra;
    out.push({ name, method, data: buf.subarray(start, start + csize), usize });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

// Parse one .npy file. Supports the dtypes a Gates save actually contains:
// little-endian int32, float32, and unicode strings (the annotations blob).
export function parseNpy(bytes) {
  if (bytes[0] !== 0x93 || td.decode(bytes.subarray(1, 6)) !== "NUMPY") {
    throw new Error("not an .npy member");
  }
  const major = bytes[6];
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const hlen = major === 1 ? dv.getUint16(8, true) : dv.getUint32(8, true);
  const hstart = major === 1 ? 10 : 12;
  const header = td.decode(bytes.subarray(hstart, hstart + hlen));
  const descr = /'descr':\s*'([^']+)'/.exec(header)[1];
  if (/'fortran_order':\s*True/.test(header)) throw new Error("fortran order unsupported");
  const shapeTxt = /'shape':\s*\(([^)]*)\)/.exec(header)[1];
  const shape = shapeTxt.split(",").map(s => s.trim()).filter(Boolean).map(Number);
  // Copy out so the typed array is aligned and owns its buffer.
  const raw = bytes.slice(hstart + hlen).buffer;

  let data;
  if (descr === "<i4") data = new Int32Array(raw);
  else if (descr === "<f4") data = new Float32Array(raw);
  else if (/^<U\d+$/.test(descr)) {
    // numpy unicode is fixed-width UTF-32LE, NUL-padded.
    const cps = new Uint32Array(raw);
    let s = "";
    for (const c of cps) { if (c === 0) break; s += String.fromCodePoint(c); }
    data = s;
  } else {
    throw new Error(`unsupported dtype ${descr}`);
  }
  return { descr, shape, data };
}

// Load every array in an .npz. Returns { name: {descr, shape, data} }.
export async function readNpz(arrayBuffer) {
  const buf = new Uint8Array(arrayBuffer);
  const out = {};
  for (const ent of zipEntries(buf)) {
    if (!ent.name.endsWith(".npy")) continue;
    let bytes;
    if (ent.method === 0) bytes = ent.data;
    else if (ent.method === 8) bytes = await inflate(ent.data);
    else throw new Error(`${ent.name}: unsupported zip method ${ent.method}`);
    out[ent.name.slice(0, -4)] = parseNpy(bytes);
  }
  return out;
}
