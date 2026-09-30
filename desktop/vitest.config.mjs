import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

export default {
  root: here,
  resolve: {
    alias: [
      { find: '@hermes/plugin-sdk', replacement: path.join(here, 'sdk-stub.mjs') }
    ]
  },
  test: {
    name: 'codex-account-switch',
    environment: 'jsdom',
    globals: true,
    setupFiles: [path.join(here, 'test-setup.mjs')],
    include: ['plugin.test.mjs'],
    testTimeout: 15_000,
    clearMocks: true
  }
}
