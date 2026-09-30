// Explicit test fixtures only. These are not live backend responses.

export const CODEX_ACCOUNTS = [
  { key: 'personal', label: 'Personal', email: 'personal@example.invalid', priority: 0 },
  { key: 'work', label: 'Work', email: 'work@example.invalid', priority: 1 }
]

export const CLAUDE_ACCOUNTS = [
  { key: 'anthropic', label: 'Anthropic', email: 'first@example.invalid', logged_in: true, cached_email: null },
  { key: 'gmail', label: 'Gmail', email: 'second@example.invalid', logged_in: true, cached_email: null },
  { key: 'work', label: 'Work', email: 'third@example.invalid', logged_in: false, cached_email: null }
]

const SCOPE = 'All Hermes profiles on this PC'

export const STATUS_PERSONAL_IDLE = {
  codex: { selected: 'personal', accounts: CODEX_ACCOUNTS },
  claude: { selected: null, state: 'unset', accounts: CLAUDE_ACCOUNTS },
  blockers: [],
  in_progress: false,
  last_operation: null,
  scope: SCOPE
}

export const STATUS_CLAUDE_GMAIL = {
  ...STATUS_PERSONAL_IDLE,
  claude: { selected: 'gmail', state: 'set', accounts: CLAUDE_ACCOUNTS }
}

export const STATUS_VERIFIED_Work = {
  ...STATUS_PERSONAL_IDLE,
  codex: { selected: 'work', accounts: CODEX_ACCOUNTS },
  last_operation: { state: 'complete', message: 'Codex Work verified after restart.' }
}

export const STATUS_FAILED = {
  ...STATUS_PERSONAL_IDLE,
  last_operation: { state: 'failed', message: 'The account switch failed.' }
}

export const STATUS_PENDING = {
  ...STATUS_PERSONAL_IDLE,
  in_progress: true,
  blockers: ['An account switch is already in progress.'],
  last_operation: { state: 'accepted', message: 'The switch request was accepted.' }
}

export function preflightFor(status, blockers = []) {
  return { ok: blockers.length === 0, blockers, codex: status.codex, claude: status.claude, scope: SCOPE }
}

export const PREFLIGHT_BLOCKED = preflightFor(STATUS_PERSONAL_IDLE, ['A Hermes profile is currently handling active work.'])

export const SWITCH_ACCEPTED = { accepted: true, operation_id: 'op-pending' }

export const SWITCH_ERROR_DETAIL = 'The backend refused this switch safely.'
