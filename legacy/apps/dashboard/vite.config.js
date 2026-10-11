import { defineConfig } from 'vite';

// Keep API calls on the browser's origin so the existing HttpOnly session works
// in development as it does behind the production dashboard proxy.
export default defineConfig(() => {
  const target = process.env.RAILSHOT_DEV_API_TARGET || 'http://127.0.0.1:4173';
  return {
    server: {
      proxy: {
        '/api/': { target, changeOrigin: true },
        '^/onpremise/install\\.sh(?:\\?|$)': { target, changeOrigin: true },
      },
    },
  };
});
