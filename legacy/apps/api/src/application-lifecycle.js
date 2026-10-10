import { EnvironmentError } from './environments.js';

export const lifecycleActions = ['stop', 'start', 'delete'];
export const lifecycleId = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export const lifecycleHash = /^[a-f0-9]{64}$/;
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
export const exact = (value, required, optional = []) => object(value)
  && required.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => [...required, ...optional].includes(key));
const name = (value) => typeof value === 'string' && /^[A-Za-z0-9_][A-Za-z0-9._:-]{0,252}$/.test(value);
const invalid = () => new EnvironmentError('APPLICATION_LIFECYCLE_RECEIPT_INVALID', 502, true);

// Only public resource identifiers and bounded states cross the executor boundary.
export function lifecycleResources(rows) {
  if (!Array.isArray(rows) || rows.length > 256) throw invalid();
  return rows.map((row) => {
    if (!object(row) || !name(row.kind) || !name(row.name) || row.namespace !== undefined && !name(row.namespace)) throw invalid();
    return { kind: row.kind, name: row.name, ...(row.namespace === undefined ? {} : { namespace: row.namespace }) };
  });
}
export function lifecycleSteps(rows) {
  if (!Array.isArray(rows) || rows.length > 256) throw invalid();
  return rows.map((row) => {
    if (!object(row) || !name(row.name) || !['queued', 'running', 'succeeded', 'blocked', 'unknown', 'skipped'].includes(row.status)) throw invalid();
    return { name: row.name, status: row.status };
  });
}
