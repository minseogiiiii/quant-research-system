import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
// Development candidate only; never expose the loopback gateway publicly.
export default defineConfig({plugins:[react()],server:{host:'127.0.0.1',port:5174,strictPort:true,
 proxy:{'/q292':{target:'http://127.0.0.1:8789',rewrite:path=>path.replace(/^\/q292/,'')}}},
 build:{outDir:'dist-fincept-candidate',rollupOptions:{input:'resilient.html'}}});
