import { createHash } from 'node:crypto';
import { promisify } from 'node:util';
import yauzl from 'yauzl';
import { archiveLimits, privateSourcePath, validateFiles } from './archive.js';

const openZip = promisify(yauzl.fromBuffer);
export const sourceSnapshotLimit = 140 * 1024 * 1024;
const require = (ok) => { if (!ok) throw new Error('Final source snapshot verification failed'); };
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
function safePath(path) {
  require(typeof path === 'string' && path.length > 0 && path.length <= 1024 && Buffer.from(path).toString('utf8') === path
    && !/[\x00-\x1f\x7f\\]/.test(path));
  const parts = path.split('/');
  require(parts.length <= 32 && !privateSourcePath(path)
    && parts.every((part) => part && !['.', '..', '.git', 'node_modules', '__MACOSX', '.DS_Store'].includes(part)));
}

export function readSourceSnapshot(bytes, publication, sourceSha256) {
  require(Buffer.isBuffer(bytes) && bytes.length <= sourceSnapshotLimit && /^[a-f0-9]{64}$/.test(sourceSha256));
  const value = JSON.parse(bytes.toString('utf8'));
  require(exact(value, ['version', 'source_commit', 'app', 'tenant', 'target_id', 'run_id', 'producer_attempt', 'source_sha256', 'entries'])
    && value.version === 1 && value.source_sha256 === sourceSha256
    && ['source_commit', 'app', 'tenant', 'target_id', 'run_id', 'producer_attempt'].every((key) => value[key] === publication[key])
    && Array.isArray(value.entries) && value.entries.length > 0 && value.entries.length <= 6000);
  const paths = new Set(), directories = new Set(['']), children = new Map(), files = [];
  let total = 0, count = 0;
  for (const entry of value.entries) {
    require(exact(entry, ['path', 'type', 'mode', 'size', 'content']));
    safePath(entry.path);
    require(!paths.has(entry.path) && ['f', 'd'].includes(entry.type) && Number.isInteger(entry.mode) && entry.mode >= 0 && entry.mode <= 0o777
      && Number.isSafeInteger(entry.size) && entry.size >= 0 && entry.size <= archiveLimits.maxBytes);
    paths.add(entry.path);
    const parent = entry.path.includes('/') ? entry.path.slice(0, entry.path.lastIndexOf('/')) : '';
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(entry);
    if (entry.type === 'd') { require(entry.size === 0 && entry.content === null); directories.add(entry.path); }
    else {
      total += entry.size; count++;
      require(count <= archiveLimits.maxFiles && total <= archiveLimits.maxBytes && typeof entry.content === 'string'
        && entry.content.length === 4 * Math.ceil(entry.size / 3));
    }
  }
  require(count > 0 && [...children.keys()].every((parent) => directories.has(parent)));
  const digest = createHash('sha256').update('railshot-source-v1\0'), visited = [];
  function visit(parent) {
    // UTF-8 lexical ordering matches Python's Unicode codepoint ordering.
    for (const entry of (children.get(parent) || []).sort((a, b) => Buffer.compare(Buffer.from(a.path), Buffer.from(b.path)))) {
      visited.push(entry.path);
      // bundle.source_digest uses Python json.dumps with ensure_ascii=True.
      const header = Buffer.from(JSON.stringify([entry.path, entry.type, entry.mode, entry.size])
        .replace(/[\x7f-\uffff]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`));
      const size = Buffer.alloc(8); size.writeBigUInt64BE(BigInt(header.length)); digest.update(size).update(header);
      if (entry.type === 'f') {
        const content = Buffer.from(entry.content, 'base64');
        require(content.length === entry.size && content.toString('base64') === entry.content);
        digest.update(content); files.push({ path: entry.path, content });
      } else visit(entry.path);
    }
  }
  visit('');
  require(visited.length === value.entries.length && visited.every((path, index) => path === value.entries[index].path)
    && digest.digest('hex') === sourceSha256);
  return validateFiles(files);
}

export async function readSourceArchive(bytes, publication, sourceSha256, expectedSnapshotSha256 = null) {
  require(Buffer.isBuffer(bytes) && bytes.length > 0 && bytes.length <= sourceSnapshotLimit);
  const zip = await openZip(bytes, { lazyEntries: true, decodeStrings: true, validateEntrySizes: true });
  try {
    require(zip.entryCount === 1);
    const snapshot = await new Promise((resolve, reject) => {
      let content = null;
      zip.once('error', reject); zip.once('end', () => content ? resolve(content) : reject(new Error('Missing source snapshot')));
      zip.on('entry', (entry) => {
        const mode = (entry.externalFileAttributes >>> 16) & 0o170000;
        if (entry.fileName !== 'snapshot.json' || ![0, 0o100000].includes(mode) || entry.generalPurposeBitFlag & 1
          || entry.uncompressedSize > sourceSnapshotLimit) { reject(new Error('Invalid source archive')); zip.close(); return; }
        zip.openReadStream(entry, async (error, stream) => {
          if (error) { reject(error); zip.close(); return; }
          try {
            const chunks = []; let size = 0;
            for await (const chunk of stream) {
              size += chunk.length; require(size <= sourceSnapshotLimit && size <= entry.uncompressedSize); chunks.push(chunk);
            }
            require(size === entry.uncompressedSize); content = Buffer.concat(chunks); zip.readEntry();
          } catch (cause) { reject(cause); zip.close(); }
        });
      });
      zip.readEntry();
    });
    if (expectedSnapshotSha256 !== null) require(createHash('sha256').update(snapshot).digest('hex') === expectedSnapshotSha256);
    return readSourceSnapshot(snapshot, publication, sourceSha256);
  } finally { zip.close(); }
}

export async function readSourceResponse(response, maxBytes = sourceSnapshotLimit) {
  if (!response.ok || Number(response.headers.get('content-length')) > maxBytes) {
    await response.body?.cancel(); throw new Error('Invalid source response');
  }
  const chunks = []; let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length; require(size <= maxBytes); chunks.push(Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}
