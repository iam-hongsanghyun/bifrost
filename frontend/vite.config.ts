import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies /api to Bifrost. Ragnarok is NOT proxied: the Ragnarok
// tab embeds it by origin and subscribes to its event stream directly, so a proxy
// would add a hop and break SSE for no benefit.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8100' } },
});
