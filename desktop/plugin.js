import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  haptic
} from '@hermes/plugin-sdk'
// `host` is read through the namespace: a named import of an export an older Desktop lacks fails the whole plugin.
import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'codex-account-switch'
// 1.1.1: managed launcher support, explicit preflight errors, dated last-attempt notices.
const STATUS_POLL_INTERVAL_MS = 10_000
const DEFAULT_SCOPE = 'All Hermes profiles on this PC'
const PENDING_STATES = new Set(['accepted', 'checking', 'restarting', 'applying', 'reopening', 'starting', 'verifying'])

const CLAUDE_UNSET = ''

function isAccountKey(value, accounts) {
  return accounts.some(account => account.key === value)
}

function plainString(value) {
  return typeof value === 'string' && value.trim() ? value : null
}

// Desktop sends a plugin's requests to the machine the active chat runs on. The switch exists only on
// this PC, so with a remote chat active no request is sent: a remote host answers 404, or, with the
// plugin installed there, would switch that machine. Desktop builds without activeConnectionId behave as before.
const REMOTE_TEXT = 'The chat you have open runs on another machine, so this button cannot reach this PC. Open a chat on This device, then press Recheck.'

class RemoteChat extends Error {}

function remoteConnection() {
  try {
    const id = sdk.host?.activeConnectionId?.()
    return typeof id === 'string' && id.trim() && id !== 'local' ? id : null
  } catch {
    return null
  }
}

function localOnly(rest) {
  return (path, opts) => remoteConnection() ? Promise.reject(new RemoteChat(REMOTE_TEXT)) : rest(path, opts)
}

function isNotFound(error) {
  const values = [error?.status, error?.statusCode, error?.response?.status, error?.detail, error?.message, error]
  return values.some(value => value === 404 || (typeof value === 'string' && /(?:^|\D)404(?:\D|$)/.test(value)))
}

function errorText(error, fallback) {
  if (isNotFound(error)) {
    return 'The account-switch backend is not available (404). If the chat you have open runs on another machine, open a chat on This device and press Recheck. Otherwise the plugin may not be enabled for this profile, or Hermes Desktop needs one restart.'
  }
  const candidate =
    error && typeof error === 'object' ? plainString(error.detail) || plainString(error.message) : plainString(error)
  return (candidate && backendSentence(candidate)) || fallback
}

// Hermes Desktop hands a failed request over as one string with the backend's answer at its end:
// Error invoking remote method 'hermes:api': Error: 409: {"detail":"..."}. Only the sentence is for the reader.
function backendSentence(text) {
  const start = text.indexOf('{')
  if (start >= 0) {
    try {
      const detail = JSON.parse(text.slice(start))?.detail
      if (plainString(detail)) return detail
    } catch {
      // Not the backend's answer; the text is shown without the wrapping.
    }
  }
  return text.replace(/^(?:Error invoking remote method '[^']*':\s*)?(?:Error:\s*)?(?:\d{3}:\s*)?/, '').trim() || null
}

function readAccounts(accounts) {
  return Array.isArray(accounts) ? accounts.filter(row => row && typeof row.key === 'string' &&
    plainString(row.label) && plainString(row.email)).map(row => ({
    key: row.key, label: row.label, email: row.email, logged_in: row.logged_in === true
  })) : []
}

function normalizeLastOperation(operation) {
  if (!operation || typeof operation !== 'object' || Array.isArray(operation)) {
    return null
  }
  const timestamp = operation.finished_at || operation.started_at
  return { state: plainString(operation.state) || '', message: plainString(operation.message),
    timestamp: typeof timestamp === 'number' && Number.isFinite(timestamp) && timestamp > 0 && timestamp < 8640000000000 ? timestamp : null }
}

function normalizeSelections(response) {
  const codex = response?.codex
  const claude = response?.claude
  const codexAccounts = readAccounts(codex?.accounts)
  const claudeAccounts = readAccounts(claude?.accounts)
  const codexValid = codex && typeof codex === 'object' && (codex.selected === null || isAccountKey(codex.selected, codexAccounts)) && Array.isArray(codex.accounts)
  const claudeValid =
    claude && typeof claude === 'object' && (claude.selected === null || isAccountKey(claude.selected, claudeAccounts)) &&
    ['unset', 'set', 'mixed'].includes(claude.state) && Array.isArray(claude.accounts)
  return {
    codex: {
      selected: isAccountKey(codex?.selected, codexAccounts) ? codex.selected : null,
      accounts: codexAccounts,
      // Which account is really paying once dead/exhausted logins are skipped. Older backends omit it.
      effective: isAccountKey(codex?.effective, codexAccounts) ? codex.effective : null,
      warnings: Array.isArray(codex?.warnings) ? codex.warnings.filter(value => typeof value === 'string' && value.trim()) : []
    },
    claude: {
      selected: isAccountKey(claude?.selected, claudeAccounts) ? claude.selected : null,
      state: claudeValid ? claude.state : null,
      accounts: claudeAccounts
    },
    valid: Boolean(codexValid && claudeValid)
  }
}

function normalizeStatus(response) {
  const shape = response && typeof response === 'object' && !Array.isArray(response)
  const operationValid = response?.last_operation === null || (response?.last_operation && typeof response.last_operation === 'object')
  const selections = normalizeSelections(response)
  return {
    ...selections,
    last_operation: normalizeLastOperation(response?.last_operation),
    in_progress: response?.in_progress === true,
    scope: plainString(response?.scope) || DEFAULT_SCOPE,
    setup_needed: response?.setup_needed === true,
    valid: Boolean(shape && selections.valid && operationValid && Array.isArray(response.blockers))
  }
}

function normalizePreflight(response) {
  const shape = response && typeof response === 'object' && !Array.isArray(response)
  const selections = normalizeSelections(response)
  return {
    ...selections,
    blockers: Array.isArray(response?.blockers) ? response.blockers.filter(value => typeof value === 'string' && value.trim()) : [],
    valid: Boolean(shape && selections.valid && Array.isArray(response.blockers) && typeof response.ok === 'boolean')
  }
}

function codexLabel(status) {
  const label = key => status.codex.accounts.find(account => account.key === key)?.label
  // A dead preferred login must never hide behind the configured order: show who is actually paying.
  if (status.codex.warnings?.length) return `⚠ ${label(status.codex.effective) || 'Check logins'}`
  return label(status.codex.selected) || 'Unknown'
}

function claudeLabel(status) {
  if (status.claude.state === 'unset') return 'Unset'
  if (status.claude.state === 'mixed') return 'Mixed'
  return status.claude.accounts.find(account => account.key === status.claude.selected)?.label || 'Unknown'
}

function isPendingOperation(operation) {
  return Boolean(operation && PENDING_STATES.has(operation.state))
}

function operationNotice(operation) {
  if (!operation) return null
  if (operation.state === 'complete' || operation.state === 'verified') {
    return { kind: 'success', message: operation.message || 'Account switch complete.' }
  }
  if (operation.state === 'failed') {
    // The reader's own language and time zone, never the author's.
    const when = operation.timestamp ? new Date(operation.timestamp * 1000).toLocaleString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
    }) : 'time unavailable'
    return { kind: 'error', message: `Last switch attempt (${when}): ${operation.message || 'The account switch failed.'}` }
  }
  const message = operation.message || 'Account switch is still pending.'
  return {
    kind: 'pending',
    message: `${message} The Hermes gateway and Hermes Desktop may close and reopen; the switch is only done after status reports Complete.`
  }
}

function noticeClass(kind) {
  if (kind === 'error') return 'border-destructive/30 bg-destructive/10 text-destructive'
  if (kind === 'success') return 'border-(--ui-accent) bg-(--ui-bg-quaternary) text-(--ui-text-primary)'
  return 'border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) text-(--ui-text-secondary)'
}

function OperationNotice({ notice }) {
  if (!notice) return null
  return jsx('div', {
    'aria-live': 'polite',
    className: `rounded-md border px-3 py-2 text-xs ${noticeClass(notice.kind)}`,
    role: notice.kind === 'error' ? 'alert' : 'status',
    children: notice.message
  })
}

function BlockedStatus({ blockers }) {
  const visibleBlockers = blockers.length > 0 ? blockers : ['The backend did not clear this switch.']
  return jsxs('div', {
    className: 'rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive',
    role: 'alert',
    children: [
      jsx('div', { className: 'font-medium', children: 'Switch blocked' }),
      jsx('ul', {
        className: 'mt-1 list-disc space-y-1 pl-4',
        children: visibleBlockers.map((blocker, index) => jsx('li', { children: blocker }, `${blocker}-${index}`))
      })
    ]
  })
}

const SELECT_CLASS =
  'w-full rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary) px-2.5 py-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50'

function AccountSelect({ id, label, value, onChange, options, disabled, help }) {
  return jsxs('div', {
    className: 'space-y-1.5',
    children: [
      jsx('label', { className: 'text-xs font-medium text-(--ui-text-primary)', htmlFor: id, children: label }),
      jsx('select', {
        'aria-describedby': `${id}-help`,
        'aria-label': label,
        className: SELECT_CLASS,
        disabled,
        id,
        onChange: event => onChange(event.target.value),
        value,
        children: options.map(option => jsx('option', { value: option.value, disabled: option.disabled, children: option.text }, option.value))
      }),
      jsx('p', { className: 'text-[0.6875rem] leading-4 text-(--ui-text-tertiary)', id: `${id}-help`, children: help })
    ]
  })
}

function SwitchDialog({
  open, onOpenChange, status, preflight, checkState, checkError, codexTarget, claudeTarget,
  onCodexChange, onClaudeChange, onRecheck, localPending, operationNotice: notice, switchError, onConfirm
}) {
  const pendingFromStatus = isPendingOperation(status.last_operation) || status.in_progress
  const pendingNotice = notice?.kind === 'pending'
  const blockers = preflight?.blockers || []
  const busy = localPending || pendingNotice || blockers.length > 0 || pendingFromStatus
  const current = preflight || status
  const codexChanged = isAccountKey(codexTarget, current.codex.accounts) && codexTarget !== current.codex.selected
  const claudeChanged = isAccountKey(claudeTarget, current.claude.accounts) && claudeTarget !== current.claude.selected
  const canConfirm = checkState === 'ready' && !checkError && !busy && (codexChanged || claudeChanged)
  const codexOptions = current.codex.accounts.map(account => ({ value: account.key, text: `${account.label} — ${account.email}` }))
  const claudeOptions = [
    ...(current.claude.state === 'set' ? [] : [{ value: CLAUDE_UNSET, text: current.claude.state === 'mixed' ? 'Mixed — profiles disagree' : 'Unset — Claude Code’s own login', disabled: true }]),
    ...current.claude.accounts.map(account => ({
      value: account.key,
      disabled: !account.logged_in,
      text: `${account.label} — ${account.email}${account.logged_in ? '' : ' (not logged in)'}`
    }))
  ]
  const selectorsDisabled = localPending || pendingNotice || pendingFromStatus

  return jsx(Dialog, {
    onOpenChange,
    open,
    children: jsx(DialogContent, {
      className: 'max-w-md',
      children: [
        jsxs(DialogHeader, {
          children: [
            jsx(DialogTitle, { children: 'Switch accounts on this PC' }),
            jsx(DialogDescription, {
              children: jsxs('span', {
                className: 'space-y-2',
                children: [
                  jsx('span', {
                    className: 'block',
                    children: `This changes the Codex and Claude accounts used by ${status.scope.charAt(0).toLowerCase()}${status.scope.slice(1)}. The Hermes gateway will restart and Hermes Desktop will close and reopen; saved conversations stay in place.`
                  }),
                  jsx('span', { className: 'block', children: 'Remote hosts, the standalone Codex app and Claude Code itself are unaffected.' })
                ]
              })
            })
          ]
        }, 'header'),
        checkError
          ? jsx('div', {
              className: 'rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive',
              role: 'alert',
              children: checkError
            }, 'check-error')
          : null,
        (status.codex.warnings || []).length
          ? jsxs('div', {
              className: 'rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive',
              role: 'alert',
              children: [
                jsx('div', { className: 'font-medium', children: 'Codex login problem' }, 'title'),
                jsx('ul', { className: 'mt-1 list-disc pl-4', children: status.codex.warnings.map(text => jsx('li', { children: text }, text)) }, 'list')
              ]
            }, 'codex-warnings')
          : null,
        jsx(OperationNotice, { notice }, 'operation'),
        switchError
          ? jsx('div', {
              className: 'rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive',
              role: 'alert',
              children: switchError
            }, 'switch-error')
          : null,
        checkState === 'checking'
          ? jsx('p', { 'aria-live': 'polite', className: 'text-xs text-(--ui-text-secondary)', children: 'Checking for active Hermes work…' }, 'checking')
          : null,
        checkState === 'error'
          ? jsx(Button, { onClick: onRecheck, type: 'button', variant: 'secondary', children: 'Recheck' }, 'check-retry')
          : null,
        checkState === 'ready'
          ? jsxs('div', {
              className: 'space-y-3',
              children: [
                current.codex.accounts.length ? jsx(AccountSelect, {
                  id: 'codex-account-switch-codex', label: 'Target Codex account', value: codexTarget, onChange: onCodexChange,
                  options: codexOptions, disabled: selectorsDisabled, help: 'Every profile’s Codex login order changes together.'
                }, 'codex') : null,
                current.claude.accounts.length ? jsx(AccountSelect, {
                  id: 'codex-account-switch-claude', label: 'Target Claude account', value: claudeTarget, onChange: onClaudeChange,
                  options: claudeOptions, disabled: selectorsDisabled,
                  help: 'Which Claude subscription the Hermes Claude option bills. Accounts marked not logged in need signing in first: setup.cmd claude <key>.'
                }, 'claude') : null,
                !current.codex.accounts.length && !current.claude.accounts.length ? jsx('p', { role: 'status', children: 'Set up account-switch settings before switching.' }, 'setup') : null,
                blockers.length > 0
                  ? jsx(BlockedStatus, { blockers }, 'blocked')
                  : pendingFromStatus || pendingNotice || localPending
                    ? null
                    : jsx('p', {
                        className: 'text-xs text-(--ui-text-secondary)',
                        children: codexChanged || claudeChanged ? 'No active Hermes work blocked the restart check.' : 'Pick a different Codex or Claude account to enable the switch.'
                      }, 'choice-hint'),
                jsx(Button, { disabled: localPending, onClick: onRecheck, type: 'button', variant: 'secondary', children: 'Recheck' }, 'recheck')
              ]
            }, 'select')
          : null,
        jsxs(DialogFooter, {
          children: [
            jsx(Button, { onClick: () => onOpenChange(false), type: 'button', variant: 'ghost', children: 'Cancel' }, 'cancel'),
            jsx(Button, {
              disabled: !canConfirm,
              onClick: onConfirm,
              type: 'button',
              children: localPending || pendingNotice || pendingFromStatus ? 'Pending…' : 'Switch account'
            }, 'switch')
          ]
        }, 'footer')
      ]
    })
  })
}

// First run: no settings yet. The dialog shows what was found on this PC and asks only for names.
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,23}$/
const INPUT_CLASS =
  'w-full rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary) px-2.5 py-1.5 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-[0.1875rem] focus-visible:ring-ring/50 disabled:opacity-50'

function normalizeFound(response) {
  const rows = (list, id) => (Array.isArray(list) ? list : [])
    .filter(row => row && plainString(row.email) && plainString(row[id]))
    .map(row => ({ id: row[id], email: row.email, name: plainString(row.name) || '', verified: row.verified !== false, set_up: row.set_up === true }))
  return {
    codex: rows(response?.codex, 'email'),
    claude: rows(response?.claude, 'key'),
    gateway_service: plainString(response?.gateway_service),
    setup_command: plainString(response?.setup_command),
    name_rule: plainString(response?.name_rule) || 'use up to 24 letters, digits and spaces'
  }
}

function agentInstructions(found) {
  const command = found.setup_command || 'setup.cmd (in the plugin\'s folder)'
  return [
    'Set up the Hermes Account Switcher plugin on this Windows PC for me.',
    '',
    `The setup command is: "${command}"`,
    '(In PowerShell, put & and a space in front of the quoted path.)',
    '',
    '1. Run it with the word show after it, and show me what it prints. That changes nothing.',
    '2. Ask me what short name I want for each account it lists, for example Personal or Work.',
    '3. Run it with the word auto after it, followed by my names, each one inside quotes. A Codex account is written as "email=Name" and a Claude account as "claude:email=Name", for example: auto "me@example.com=Personal" "claude:me@example.com=Family"',
    '4. If I want a Claude account that is not listed, run it with: claude <short-name> <email>. It prints a sign-in link; I finish that sign-in in my own browser. You cannot sign in for me.',
    '5. Tell me to restart Hermes Desktop, then to click the account button at the bottom right.',
    '',
    'Do not switch accounts, do not edit or copy any login file, and do not print any token. I switch accounts myself from the account button in Hermes Desktop.'
  ].join('\n')
}

function NameRow({ kind, row, value, onChange, disabled }) {
  const id = `codex-account-switch-name-${kind}-${row.id.replace(/[^A-Za-z0-9]+/g, '-')}`
  const bad = value.trim() !== '' && !NAME_PATTERN.test(value.trim().replace(/\s+/g, ' '))
  return jsxs('div', {
    className: 'space-y-1',
    children: [
      jsx('label', { className: 'block text-xs text-(--ui-text-secondary)', htmlFor: id, children: row.verified ? row.email : `${row.email} (sign-in is checked before its first switch)` }),
      row.set_up
        ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Already set up.' })
        : jsx('input', {
            'aria-invalid': bad ? 'true' : undefined,
            'aria-label': `Name for ${row.email}`,
            className: INPUT_CLASS,
            disabled,
            id,
            maxLength: 24,
            onChange: event => onChange(event.target.value),
            placeholder: row.name,
            type: 'text',
            value
          })
    ]
  })
}

function SetupDialog({ open, onOpenChange, found, foundError, names, onName, onSave, onRetry, saving, agentText, onAgent }) {
  const fresh = found ? [...found.codex.map(row => ['codex', row]), ...found.claude.map(row => ['claude', row])].filter(([, row]) => !row.set_up) : []
  const invalid = fresh.some(([kind, row]) => {
    const text = (names[`${kind}:${row.id}`] ?? row.name).trim().replace(/\s+/g, ' ')
    return text !== '' && !NAME_PATTERN.test(text)
  })
  const nothing = found && found.codex.length === 0 && found.claude.length === 0
  const switchable = found && (found.codex.length > 1 || found.claude.length > 1)
  const section = (title, kind, rows, empty) => jsxs('div', {
    className: 'space-y-2',
    children: [
      jsx('div', { className: 'text-xs font-medium text-(--ui-text-primary)', children: title }, 'title'),
      rows.length
        ? rows.map(row => jsx(NameRow, {
            kind, row, disabled: saving, value: names[`${kind}:${row.id}`] ?? row.name,
            onChange: value => onName(`${kind}:${row.id}`, value)
          }, row.id))
        : jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: empty }, 'empty')
    ]
  }, kind)

  return jsx(Dialog, {
    onOpenChange,
    open,
    children: jsx(DialogContent, {
      className: 'max-w-md',
      children: [
        jsxs(DialogHeader, {
          children: [
            jsx(DialogTitle, { children: 'Set up account switching' }),
            jsx(DialogDescription, {
              children: 'These are the accounts already signed in on this PC. Each box holds a suggested name: type over it to change it. Save keeps the names and opens the switch dialog. Nothing is switched or signed in by saving.'
            })
          ]
        }, 'header'),
        foundError
          ? jsx('div', {
              className: 'rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive',
              role: 'alert',
              children: foundError
            }, 'found-error')
          : null,
        !found && !foundError
          ? jsx('p', { 'aria-live': 'polite', className: 'text-xs text-(--ui-text-secondary)', children: 'Looking for your accounts…' }, 'looking')
          : null,
        !found && foundError
          ? jsx(Button, { onClick: onRetry, type: 'button', variant: 'secondary', children: 'Look again' }, 'retry')
          : null,
        found
          ? jsxs('div', {
              className: 'space-y-4',
              children: [
                section('Codex accounts', 'codex', found.codex, 'No Codex account is signed in to Hermes. Sign in with "hermes auth add openai-codex", once per account.'),
                section('Claude accounts', 'claude', found.claude, 'No Claude account is signed in for switching. Add one with "setup.cmd claude <name> <email>", or hand the instructions below to your agent.'),
                jsx('p', {
                  className: 'text-xs text-(--ui-text-tertiary)',
                  children: `Gateway: ${found.gateway_service ? `Windows service ${found.gateway_service}` : 'started by Hermes itself'}. For a name, ${found.name_rule}.`
                }, 'gateway'),
                !nothing && !switchable
                  ? jsx('p', { className: 'text-xs text-(--ui-text-secondary)', role: 'status', children: 'There is nothing to switch between yet: that takes two accounts of the same kind.' }, 'single')
                  : null
              ]
            }, 'found')
          : null,
        agentText
          ? jsxs('div', {
              className: 'space-y-1',
              children: [
                jsx('p', { className: 'text-xs text-(--ui-text-secondary)', role: 'status', children: 'Copied. Paste this to your agent:' }, 'copied'),
                jsx('textarea', { 'aria-label': 'Instructions for your agent', className: `${INPUT_CLASS} text-xs`, readOnly: true, rows: 7, style: { minHeight: '8rem' }, value: agentText }, 'text')
              ]
            }, 'agent')
          : null,
        jsxs(DialogFooter, {
          children: [
            jsx(Button, { disabled: !found, onClick: onAgent, type: 'button', variant: 'secondary', children: 'Copy instructions for my agent' }, 'agent'),
            jsx(Button, { onClick: () => onOpenChange(false), type: 'button', variant: 'ghost', children: 'Cancel' }, 'cancel'),
            jsx(Button, { disabled: !found || nothing || invalid || saving, onClick: onSave, type: 'button', children: saving ? 'Saving…' : 'Save' }, 'save')
          ]
        }, 'footer')
      ]
    })
  })
}

// Before the first successful poll, and after a failed one, both selections are unknown — never "Unset".
const EMPTY_STATUS = normalizeStatus({ codex: { selected: null, accounts: [] }, claude: { selected: null, state: null, accounts: [] }, last_operation: null, in_progress: false, blockers: [], scope: DEFAULT_SCOPE })

function AccountStatusButton({ rest }) {
  const [status, setStatus] = useState(EMPTY_STATUS)
  const [statusError, setStatusError] = useState(null)
  const [remote, setRemote] = useState(false)
  const [preflight, setPreflight] = useState(null)
  const [checkState, setCheckState] = useState('idle')
  const [checkError, setCheckError] = useState(null)
  const [open, setOpen] = useState(false)
  const [codexTarget, setCodexTarget] = useState('')
  const [claudeTarget, setClaudeTarget] = useState(CLAUDE_UNSET)
  const [localPending, setLocalPending] = useState(false)
  const [switchError, setSwitchError] = useState(null)
  const [operationNoticeState, setOperationNoticeState] = useState(null)
  const [found, setFound] = useState(null)
  const [foundError, setFoundError] = useState(null)
  const [names, setNames] = useState({})
  const [saving, setSaving] = useState(false)
  const [agentText, setAgentText] = useState(null)
  const disposed = useRef(false)

  async function refreshStatus() {
    try {
      const response = await rest('/status')
      if (disposed.current) return
      const next = normalizeStatus(response)
      setRemote(false)
      setStatus(next)
      setStatusError(next.valid ? null : 'The account status response was incomplete; confirmation is disabled.')
      const notice = operationNotice(next.last_operation)
      setOperationNoticeState(current => notice || (current?.kind === 'pending' ? current : null))
      if (notice?.kind === 'success' || notice?.kind === 'error') {
        setLocalPending(false)
        setSwitchError(null)
      }
      return next
    } catch (error) {
      if (disposed.current) return
      setRemote(error instanceof RemoteChat)
      setStatus(EMPTY_STATUS)
      setStatusError(errorText(error, 'Unable to read account status.'))
    }
    return null
  }

  async function lookForAccounts() {
    setFound(null)
    setFoundError(null)
    setAgentText(null)
    try {
      const next = normalizeFound(await rest('/setup'))
      if (disposed.current) return
      setFound(next)
      setNames({})
    } catch (error) {
      if (disposed.current) return
      setFoundError(errorText(error, 'Unable to look for the accounts on this PC.'))
    }
  }

  async function saveSetup() {
    if (!found || saving) return
    const given = (kind, rows) => Object.fromEntries(rows.filter(row => !row.set_up)
      .map(row => [row.id, (names[`${kind}:${row.id}`] ?? row.name).trim().replace(/\s+/g, ' ') || row.name]))
    setSaving(true)
    setFoundError(null)
    try {
      const response = await rest('/setup', { method: 'POST', body: { confirmed: true, codex: given('codex', found.codex), claude: given('claude', found.claude) } })
      if (response?.ok !== true) throw new Error('The settings were not written.')
      if (disposed.current) return
      const next = await refreshStatus()
      if (next && !next.setup_needed) void runPreflight()
    } catch (error) {
      if (disposed.current) return
      setFoundError(errorText(error, 'The settings could not be written.'))
    } finally {
      if (!disposed.current) setSaving(false)
    }
  }

  async function copyForAgent() {
    if (!found) return
    const text = agentInstructions(found)
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // No clipboard here: the text is shown to be copied by hand.
    }
    if (!disposed.current) setAgentText(text)
  }

  async function openChecks() {
    const next = await refreshStatus()
    if (disposed.current) return
    if (next?.setup_needed) await lookForAccounts()
    else await runPreflight()
  }

  async function runPreflight() {
    setCheckState('checking')
    setCheckError(null)
    try {
      const response = await rest('/preflight')
      if (disposed.current) return
      const next = normalizePreflight(response)
      if (!next.valid) {
        setPreflight(null)
        setCheckError(response?.ok === false && next.blockers.length
          ? next.blockers.join(' ')
          : 'The pre-flight response was incomplete; confirmation is disabled.')
        setCheckState('error')
        return
      }
      if (disposed.current) return
      setPreflight(next)
      setCodexTarget(next.codex.selected || next.codex.accounts[0]?.key || '')
      setClaudeTarget(next.claude.state === 'set' && next.claude.selected ? next.claude.selected : CLAUDE_UNSET)
      setCheckState('ready')
    } catch (error) {
      if (disposed.current) return
      setPreflight(null)
      setCheckError(errorText(error, 'Unable to check for active Hermes work.'))
      setCheckState('error')
    }
  }

  useEffect(() => {
    disposed.current = false
    void refreshStatus()
    const timer = window.setInterval(() => void refreshStatus(), STATUS_POLL_INTERVAL_MS)
    return () => {
      disposed.current = true
      window.clearInterval(timer)
    }
  }, [rest])

  useEffect(() => {
    if (open) void openChecks()
  }, [open])

  async function submitSwitch() {
    const current = preflight || status
    const body = { confirmed: true }
    if (isAccountKey(codexTarget, current.codex.accounts) && codexTarget !== current.codex.selected) body.codex = codexTarget
    if (isAccountKey(claudeTarget, current.claude.accounts) && claudeTarget !== current.claude.selected) body.claude = claudeTarget
    const allowed =
      checkState === 'ready' && !checkError && !statusError && preflight && preflight.blockers.length === 0 &&
      !isPendingOperation(status.last_operation) && !status.in_progress && !localPending && (body.codex || body.claude)
    if (!allowed) return

    setSwitchError(null)
    setLocalPending(true)
    try {
      const response = await rest('/switch', { method: 'POST', body })
      if (response?.accepted !== true || !plainString(response?.operation_id)) {
        throw new Error('The backend did not accept the account switch.')
      }
      setOperationNoticeState({
        kind: 'pending',
        message: 'Switch request accepted, but it is still pending. The Hermes gateway and Hermes Desktop may close and reopen; the switch is only done after status reports Complete.'
      })
    } catch (error) {
      setLocalPending(false)
      setSwitchError(errorText(error, 'The account switch request failed.'))
    }
  }

  // A provider with no accounts set up is left off the badge; with nothing known yet, both read "Unknown".
  const hasCodex = status.codex.accounts.length > 0
  const hasClaude = status.claude.accounts.length > 0
  const statusLabel = remote ? 'Account switch: this PC only' : status.setup_needed ? 'Account switch: set up' : [
    hasCodex || !hasClaude ? `Codex: ${codexLabel(status)}` : null,
    hasClaude || !hasCodex ? `Claude: ${claudeLabel(status)}` : null
  ].filter(Boolean).join(' · ')

  return jsxs('div', {
    children: [
      jsx(Button, {
        'aria-label': statusLabel,
        // The default variant is a solid primary button; in the status bar this is quiet text.
        variant: 'ghost',
        className:
          `inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] ${status.codex.warnings?.length ? 'text-destructive' : 'text-(--ui-text-tertiary)'} transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground`,
        title: status.codex.warnings?.length ? status.codex.warnings.join('\n') : undefined,
        onClick: () => {
          haptic('tap')
          setOpen(true)
        },
        type: 'button',
        children: statusLabel
      }),
      status.setup_needed ? jsx(SetupDialog, {
        open,
        onOpenChange: setOpen,
        found,
        foundError,
        names,
        onName: (key, value) => setNames(current => ({ ...current, [key]: value })),
        onSave: () => void saveSetup(),
        onRetry: () => void lookForAccounts(),
        saving,
        agentText,
        onAgent: () => void copyForAgent()
      }) : jsx(SwitchDialog, {
        onConfirm: () => void submitSwitch(),
        onOpenChange: setOpen,
        onRecheck: () => void openChecks(),
        onCodexChange: setCodexTarget,
        onClaudeChange: setClaudeTarget,
        open,
        operationNotice: operationNoticeState,
        status,
        preflight,
        checkState,
        checkError: checkError || statusError,
        switchError,
        codexTarget,
        claudeTarget,
        localPending
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Account switch (Codex + Claude)',
  description: 'Switch the PC-wide Codex and Claude accounts used by Hermes Desktop profiles.',
  register(ctx) {
    ctx.register({
      id: 'status',
      area: 'statusBar.right',
      order: 120,
      render: () => jsx(AccountStatusButton, { rest: localOnly(ctx.rest) })
    })
  }
}
