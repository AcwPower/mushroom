import { copyFileSync, mkdirSync } from 'node:fs';
mkdirSync('dist', { recursive: true });
for (const file of ['index.html', 'style.css', 'app.js']) copyFileSync(file, `dist/${file}`);
console.log('Frontend built into dist/');
