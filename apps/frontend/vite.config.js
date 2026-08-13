import { fileURLToPath, URL } from 'node:url';
import vue from '@vitejs/plugin-vue';
import Components from 'unplugin-vue-components/vite';
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers';
import { defineConfig } from 'vite';
export default defineConfig({
    plugins: [
        vue(),
        Components({
            dts: 'src/components.d.ts',
            resolvers: [ElementPlusResolver()],
        }),
    ],
    resolve: {
        alias: {
            '@': fileURLToPath(new URL('./src', import.meta.url)),
        },
    },
    server: {
        port: 5173,
        proxy: {
            '/api': 'http://127.0.0.1:8000',
            '/healthz': 'http://127.0.0.1:8000',
            '/readyz': 'http://127.0.0.1:8000',
        },
    },
});
