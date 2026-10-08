import {defineConfig} from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],test:{environment:'jsdom',globals:true,setupFiles:['./src/testSetup.ts'],testTimeout:15000,hookTimeout:15000}});
