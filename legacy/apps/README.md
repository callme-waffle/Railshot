# Application code map

The browser, HTTP API, and MCP agent are separate entry points. The API owns validation, session state, and deployment coordination; the browser and agent call that API.

| Area | Entry point | Main responsibility |
| --- | --- | --- |
| `dashboard/` | `index.html`, `app.js` | Browser navigation, deployment views, and polling |
| `dashboard/src/api.js` | Imported by dashboard modules | Same-origin HTTP requests and cancellation |
| `dashboard/src/openstack-installer.js` | Imported by `app.js` | OpenStack registration and installer presentation |
| `dashboard/src/lifecycle.js` | Imported by `app.js` | Application start, stop, and delete controls |
| `api/src/server.js` | `npm start --prefix apps/api` | Route dispatch and service setup |
| `api/src/http/` | Imported by `server.js` | Request parsing, source upload, and response formatting |
| `api/src/openstack/` | Imported by API service | Registration tokens and installer package |
| `api/src/product.js`, `product-store.js` | Imported by `server.js` | Deployment state machine and durable storage |
| `agent/src/` | `npm run mcp --workspace @railshot/agent` or `npm run mcp:remote --workspace @railshot/agent` | stdio or OAuth protected HTTP MCP tools and product API client; optional HTTP conversation agent |
| `agent/*.py` | Called by customer install process | Restricted on-premises command channel; paths are part of `install.sh`'s package contract |

The API can run as a host Node process or from the `api` stage of `api/Dockerfile`. Its `mcp` stage packages both `agent/src/mcp.js` and `agent/src/remote-mcp.js`; the dashboard and CI runner have separate images. See [host deployment](../docs/architecture/host-api-deployment.md) or [container deployment](../docs/architecture/container-deployment.md).
