#!/usr/bin/env node
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { createToolServer } from './mcp-tools.js';
import { localTools } from './local-tools.js';

// Run on the user's computer with the repository dependencies installed.
// Existing remote/container entry points keep their repository-only tool catalog.
await createToolServer(undefined, { localTools }).connect(new StdioServerTransport());
