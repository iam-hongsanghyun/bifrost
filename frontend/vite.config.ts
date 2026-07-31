import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Ports come from the environment, with Bifrost's own defaults.
 *
 * 8770/8771 rather than Vite's 5173 and a round 8000-something: those collide with
 * whatever else the user has running — 5173 with any other Vite project, 8000 with
 * Ragnarok itself. An adjacent pair one digit apart is easy to remember together and
 * sits outside the common dev defaults (3000, 4200, 5000, 5173, 8000, 8080, 8888).
 *
 * The proxy target is DERIVED from the same variable the API is started with. It used
 * to be a literal, which meant changing BIFROST_API_PORT moved the API and left the
 * UI talking to nothing — a break with no error message, because the proxy just
 * failed to connect.
 */
const API_PORT = Number(process.env.BIFROST_API_PORT ?? 8770);
const UI_PORT = Number(process.env.BIFROST_UI_PORT ?? 8771);

// Ragnarok is NOT proxied: the Ragnarok tab embeds it by origin and subscribes to its
// event stream directly, so a proxy would add a hop and break SSE for no benefit.
export default defineConfig({
  plugins: [react()],
  server: {
    port: UI_PORT,
    // Fail rather than silently move to the next free port: a UI on an unexpected
    // port is one the launcher's readiness check and the browser it opened cannot
    // find.
    strictPort: true,
    proxy: { '/api': `http://127.0.0.1:${API_PORT}` },
  },
});
