import { createHash } from 'node:crypto';
import { APP_NAME, SOURCE_COMMIT, TARGET_ID, TENANT_NAME } from './contract.js';

const HASH = /^[a-f0-9]{64}$/;
const IMAGE = /^ghcr\.io\/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$/;
const DNS_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const RELEASE_ORDERS = [
  ['L0', 'L1', 'L2', 'L3'],
  ['L0', 'L1', 'L2', 'L4', 'L3'],
  ['L0', 'L1', 'Q', 'L2', 'L4', 'L3'],
];
const HANDOFF_FIELDS = ['version', 'status', 'source_commit', 'target_id', 'tenant', 'app',
  'run_id', 'producer_attempt', 'bundle_artifact_id', 'files', 'registry'];
const sameKeys = (a, b) => JSON.stringify(Object.keys(a || {}).sort()) === JSON.stringify(Object.keys(b || {}).sort());

function qualityAdvisory(row) {
  const error = row.error || {};
  return row.layer === 'Q' && row.advisory === true && row.ok === false
    && ['FAIL', 'BLOCKED'].includes(row.outcome) && error.outcome === row.outcome
    && (error.code === 'GATE_CONFIG_INVALID' && error.phase === 'Q.discovery'
      || ['GATE_CHECK_FAILED', 'GATE_ENVIRONMENT_UNAVAILABLE'].includes(error.code)
        && ['Q', 'Q.discovery', 'Q.prepare', 'Q.lint', 'Q.type', 'Q.unit', 'Q.report'].includes(error.phase));
}

// These hashes check the trusted workflow artifact channel, not a detached signature.
export function readPublished(files, { runId, attempt, headSha, targetId, tenant }) {
  const entries = new Map(files.map(({ path, content }) => [path, content]));
  const require = (ok, message) => { if (!ok) throw new Error(`게시 산출물 확인 실패: ${message}`); };
  const specs = ['railshot.yaml', 'jasmin.yaml'].filter((name) => entries.has(name));
  require(specs.length === 1, '단일 Railshot 또는 legacy Jasmin 명세');
  const [spec] = specs;
  const boundFiles = ['images.json', spec, 'verdict.json', 'manifest.json'];
  require(entries.size === 5 && [...boundFiles, 'handoff.json'].every((name) => entries.has(name)), '파일 구성');
  const handoff = JSON.parse(entries.get('handoff.json'));
  require(sameKeys(handoff, Object.fromEntries(HANDOFF_FIELDS.map((name) => [name, true]))), '인계 필드');
  require(handoff.version === 2 && handoff.status === 'published', '인계 상태');
  require(handoff.run_id === Number(runId) && handoff.producer_attempt === attempt, 'run/attempt 불일치');
  require(SOURCE_COMMIT.test(handoff.source_commit) && handoff.source_commit === headSha, 'source commit 불일치');
  require(typeof handoff.target_id === 'string' && TARGET_ID.test(handoff.target_id) && handoff.target_id === targetId, 'target 불일치');
  require(typeof handoff.tenant === 'string' && TENANT_NAME.test(handoff.tenant) && handoff.tenant === tenant && typeof handoff.app === 'string' && APP_NAME.test(handoff.app), 'tenant/app 불일치');
  require(Number.isSafeInteger(handoff.bundle_artifact_id) && handoff.bundle_artifact_id > 0, 'bundle artifact ID');
  require(sameKeys(handoff.files, Object.fromEntries(boundFiles.map((name) => [name, true]))), '파일 해시 목록');
  for (const name of boundFiles) {
    require(HASH.test(handoff.files[name]) && createHash('sha256').update(entries.get(name)).digest('hex') === handoff.files[name], `${name} 해시`);
  }
  const registry = handoff.registry;
  require(sameKeys(registry, { visibility: true, verification: true, images_sha256: true, image_pull_secret: true }), 'registry 필드');
  require(registry.images_sha256 === handoff.files['images.json'], 'registry 이미지 해시');
  require(['public', 'private'].includes(registry.visibility) && registry.verification ===
    (registry.visibility === 'private' ? 'authenticated_manifest_read' : 'anonymous_manifest_read'), 'registry 검증 방식');
  const secret = registry.image_pull_secret;
  require(registry.visibility === 'public' ? secret === null :
    sameKeys(secret, { namespace: true, name: true }) && Object.values(secret).every((value) =>
      typeof value === 'string' && DNS_LABEL.exec(value)?.[0] === value), 'image pull Secret 참조');
  const images = JSON.parse(entries.get('images.json'));
  const manifest = JSON.parse(entries.get('manifest.json'));
  const verdict = JSON.parse(entries.get('verdict.json'));
  require(images && !Array.isArray(images) && Object.keys(images).length > 0 && Object.values(images).every((value) => typeof value === 'string' && IMAGE.test(value)), '이미지 digest');
  require(manifest.version === 1 && manifest.trust === 'trusted-ci-artifact-not-a-signature', 'bundle 계약');
  require(HASH.test(manifest.source_sha256) && manifest.source_sha256 === verdict.source_sha256, 'source digest');
  require(manifest.files?.[spec] === handoff.files[spec] && manifest.files?.['verdict.json'] === handoff.files['verdict.json'], 'bundle 파일 해시');
  require(verdict.ok === true && verdict.release_eligible === true && verdict.status === 'PASS', 'gate 판정');
  require(Array.isArray(verdict.layers) && RELEASE_ORDERS.some((order) => verdict.layers.length === order.length
    && verdict.layers.every((row, index) => row.layer === order[index] && (qualityAdvisory(row) || row.ok === true && !row.blocked && !row.errors?.length))), 'gate 단계');
  require(sameKeys(images, manifest.images) && sameKeys(images, verdict.images) && sameKeys(images, verdict.image_ids), 'service 목록');
  for (const name of Object.keys(images)) {
    require(manifest.images[name]?.local_ref === verdict.images[name] && manifest.images[name]?.id === verdict.image_ids[name], '검사 이미지 ID');
  }
  return { ...handoff, images };
}
