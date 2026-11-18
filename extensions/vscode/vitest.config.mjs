// Unit tests: pure modules only (no `vscode` import). Run with `npm run test:unit`.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/unit/**/*.test.ts'],
    environment: 'node',
  },
});
