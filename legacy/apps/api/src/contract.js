// Shared with the CI workflow admission rules; target IDs name operator configuration.
export { APP_NAME, APP_NAME_MESSAGE, sourceAppName } from '../../../contracts/application.mjs';
export const TENANT_NAME = /^[a-z0-9]{1,20}$/;
export const TARGET_ID = /^[a-z][a-z0-9-]{0,62}$/;
export const SOURCE_COMMIT = /^[a-f0-9]{40}$/;
