#!/usr/bin/env node
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { createToolServer } from './mcp-tools.js';

await createToolServer().connect(new StdioServerTransport());
