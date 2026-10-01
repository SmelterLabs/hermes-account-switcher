import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import plugin from './plugin.js'
import {
  PREFLIGHT_BLOCKED,
  STATUS_CLAUDE_GMAIL,
  STATUS_FAILED,
  STATUS_PENDING,
  STATUS_PERSONAL_IDLE,
  STATUS_VERIFIED_Work,
  SWITCH_ACCEPTED,
  SWITCH_ERROR_DETAIL,
  preflightFor
} from './fixtures.mjs'

function sequence(values) {
  let index = 0
  return () => values[Math.min(index++, values.length - 1)]
}

function makeRest({ status = [STATUS_PERSONAL_IDLE], preflight = null, statusFailure = null, switchResponse = SWITCH_ACCEPTED } = {}) {
  const nextStatus = sequence(status)
  const nextPreflight = sequence(preflight || [preflightFor(status[0])])
  const calls = []

  const rest = async (path, opts = {}) => {
    calls.push({ path, opts })
    if (statusFailure) throw statusFailure
    if (path === '/status') return nextStatus()
    if (path === '/preflight') return nextPreflight()
    if (path === '/switch') {
      return typeof switchResponse === 'function' ? switchResponse({ path, opts }) : switchResponse
    }
    throw new Error(`Unexpected fixture path: ${path}`)
  }

  rest.calls = calls
  return rest
}

function registerWith(rest) {
  const contributions = []
  const ctx = {
    source: 'plugin:codex-account-switch',
    rest,
    register(contribution) {
      contributions.push(contribution)
      return () => {}
    },
    registerMany(items) {
      contributions.push(...items)
      return () => {}
    },
    onDispose() {}
  }
  plugin.register(ctx)
  return { statusContribution: contributions.find(item => item.area === 'statusBar.right'), contributions }
}

const count = (rest, path) => rest.calls.filter(call => call.path === path).length

async function openDialog(rest) {
  const { statusContribution } = registerWith(rest)
  render(statusContribution.render())
  const button = await screen.findByRole('button', { name: /Codex:/ })
  fireEvent.click(button)
  await screen.findByRole('dialog')
  await waitFor(() => expect(count(rest, '/preflight')).toBe(1))
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Target Codex account' })).toBeTruthy())
}

afterEach(() => {
  cleanup()
  delete window.hermesDesktop
  delete globalThis.__activeConnectionId
})

describe('codex-account-switch desktop plugin', () => {
  it('sends nothing while the active chat runs on a remote connection, and says why', async () => {
    globalThis.__activeConnectionId = 'forge'
    const rest = makeRest()
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: 'Account switch: this PC only' }))
    await screen.findByRole('dialog')
    expect((await screen.findAllByText(/runs on another machine/)).length).toBeGreaterThan(0)
    expect(rest.calls).toHaveLength(0)
  })

  it('treats the local connection, by id or by none at all, as this PC', async () => {
    for (const id of ['local', null]) {
      globalThis.__activeConnectionId = id
      const rest = makeRest()
      await openDialog(rest)
      expect(count(rest, '/status')).toBeGreaterThan(0)
      cleanup()
    }
  })

  it('reaches this PC again once a local chat is active', async () => {
    globalThis.__activeConnectionId = 'forge'
    const rest = makeRest()
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: 'Account switch: this PC only' }))
    await screen.findByRole('dialog')
    globalThis.__activeConnectionId = 'local'
    fireEvent.click(await screen.findByRole('button', { name: 'Recheck' }))
    await waitFor(() => expect(count(rest, '/preflight')).toBe(1))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Target Codex account' })).toBeTruthy())
  })

  it('uses arbitrary account keys returned by the backend', async () => {
    const status = {
      ...STATUS_PERSONAL_IDLE,
      codex: { selected: 'alpha', accounts: [
        { key: 'alpha', label: 'Alpha', email: 'alpha@example.invalid' },
        { key: 'beta', label: 'Beta', email: 'beta@example.invalid' },
        { key: 'gamma', label: 'Gamma', email: 'gamma@example.invalid' }
      ] },
      claude: { selected: null, state: 'unset', accounts: [] }
    }
    const rest = makeRest({ status: [status] })
    await openDialog(rest)
    // Seen on a real stock install: with no Claude account set up the badge still read "Claude: Unset".
    expect(screen.getByRole('button', { name: 'Codex: Alpha' })).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Target Claude account' })).toBeNull()
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'gamma' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    await waitFor(() => expect(rest.calls.find(call => call.path === '/switch')?.opts.body).toEqual({ confirmed: true, codex: 'gamma' }))
  })

  it('supports a Claude-only backend response', async () => {
    const status = {
      ...STATUS_PERSONAL_IDLE,
      codex: { selected: null, accounts: [] },
      claude: { selected: null, state: 'unset', accounts: [
        { key: 'one', label: 'One', email: 'one@example.invalid', logged_in: true },
        { key: 'two', label: 'Two', email: 'two@example.invalid', logged_in: true }
      ] }
    }
    const rest = makeRest({ status: [status] })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    // Codex has no accounts set up here, so the badge does not mention it.
    fireEvent.click(await screen.findByRole('button', { name: 'Claude: Unset' }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Target Claude account' })).toBeTruthy())
    expect(screen.queryByRole('combobox', { name: 'Target Codex account' })).toBeNull()
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Claude account' }), { target: { value: 'two' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    await waitFor(() => expect(rest.calls.find(call => call.path === '/switch')?.opts.body).toEqual({ confirmed: true, claude: 'two' }))
  })

  it('preserves a failed preflight explanation from older backends', async () => {
    const rest = makeRest({ preflight: [{ ok: false, blockers: ['No live Desktop backends were found.'] }] })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: /Codex:/ }))
    expect((await screen.findByRole('alert')).textContent).toContain('No live Desktop backends were found.')
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
    expect(count(rest, '/switch')).toBe(0)
  })

  it('dates the last failed attempt instead of presenting it as a new failure', async () => {
    const status = { ...STATUS_FAILED, last_operation: { ...STATUS_FAILED.last_operation, finished_at: 1790286035 } }
    await openDialog(makeRest({ status: [status] }))
    // Shown in the reader's own language and time zone, whatever those are on this machine.
    const when = new Date(1790286035 * 1000).toLocaleString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
    })
    expect(screen.getByRole('alert').textContent).toContain(`Last switch attempt (${when})`)
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'work' } })
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(false)
  })

  it('retains the authoritative active-work blocker without relying on private Desktop transport', async () => {
    const rest = makeRest({ preflight: [preflightFor(STATUS_PERSONAL_IDLE, ['default: gateway or scheduled work is active.'])] })
    await openDialog(rest)
    expect(screen.getByText('default: gateway or scheduled work is active.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
    expect(count(rest, '/switch')).toBe(0)
  })

  it('refreshes active-work blockers on Recheck', async () => {
    const blocker = 'default: gateway or scheduled work is active.'
    const rest = makeRest({ preflight: [preflightFor(STATUS_PERSONAL_IDLE, [blocker])] })
    await openDialog(rest)
    expect(screen.getByText(blocker)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Recheck' }))
    await waitFor(() => expect(count(rest, '/preflight')).toBe(2))
    expect(screen.getByText(blocker)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
  })

  it('leaves an idle safety result unchanged', async () => {
    await openDialog(makeRest())
    expect(screen.getByText(/Pick a different Codex or Claude account/)).toBeTruthy()
  })

  it('loads with the SDK stub, registers a status button, and polls only the cheap status route', async () => {
    const rest = makeRest()
    const { statusContribution, contributions } = registerWith(rest)
    expect(plugin.id).toBe('codex-account-switch')
    expect(contributions).toHaveLength(1)
    expect(statusContribution.area).toBe('statusBar.right')
    render(statusContribution.render())
    expect(await screen.findByRole('button', { name: 'Codex: Personal · Claude: Unset' })).toBeTruthy()
    await waitFor(() => expect(count(rest, '/status')).toBe(1))
    expect(count(rest, '/preflight')).toBe(0)
  })

  it('renders every selection state in the label', async () => {
    const cases = [
      [STATUS_CLAUDE_GMAIL, 'Codex: Personal · Claude: Gmail'],
      [{ ...STATUS_PERSONAL_IDLE, codex: { ...STATUS_PERSONAL_IDLE.codex, selected: 'work' } }, 'Codex: Work · Claude: Unset'],
      [{ ...STATUS_PERSONAL_IDLE, codex: { ...STATUS_PERSONAL_IDLE.codex, selected: null }, claude: { ...STATUS_PERSONAL_IDLE.claude, state: 'mixed' } }, 'Codex: Unknown · Claude: Mixed']
    ]
    for (const [status, label] of cases) {
      const { statusContribution } = registerWith(makeRest({ status: [status] }))
      render(statusContribution.render())
      expect(await screen.findByRole('button', { name: label })).toBeTruthy()
      cleanup()
    }
  })

  it('warns on the badge and in the dialog when the preferred Codex login is dead', async () => {
    const warning = 'alpha: Personal login is dead, so Codex is billing Work. Sign Personal in again.'
    const status = { ...STATUS_PERSONAL_IDLE, codex: { ...STATUS_PERSONAL_IDLE.codex, effective: 'work', warnings: [warning] } }
    const rest = makeRest({ status: [status], preflight: [preflightFor(status)] })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    const badge = await screen.findByRole('button', { name: 'Codex: ⚠ Work · Claude: Unset' })
    expect(badge.getAttribute('title')).toBe(warning)
    fireEvent.click(badge)
    expect(await screen.findByText(warning)).toBeTruthy()
  })

  it('fails closed when pre-flight blockers are present and never posts a switch', async () => {
    const rest = makeRest({ preflight: [PREFLIGHT_BLOCKED] })
    await openDialog(rest)
    expect(screen.getByText(/all Hermes profiles on this PC/i)).toBeTruthy()
    const confirm = screen.getByRole('button', { name: 'Switch account' })
    expect(confirm.disabled).toBe(true)
    expect(screen.getByRole('alert').textContent).toContain(PREFLIGHT_BLOCKED.blockers[0])
    fireEvent.click(confirm)
    expect(count(rest, '/switch')).toBe(0)
  })

  it('closes on Cancel without posting', async () => {
    const rest = makeRest()
    await openDialog(rest)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(count(rest, '/switch')).toBe(0)
  })

  it('posts only the changed Codex account with confirmed true', async () => {
    const rest = makeRest()
    await openDialog(rest)
    expect(screen.getByRole('option', { name: 'Personal — personal@example.invalid' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Work — work@example.invalid' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'work' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    await waitFor(() => expect(count(rest, '/switch')).toBe(1))
    expect(rest.calls.find(call => call.path === '/switch').opts).toEqual({ method: 'POST', body: { confirmed: true, codex: 'work' } })
  })

  it('posts a Claude switch on its own and refuses accounts that are not logged in', async () => {
    const rest = makeRest({ status: [STATUS_CLAUDE_GMAIL] })
    await openDialog(rest)
    const claude = screen.getByRole('combobox', { name: 'Target Claude account' })
    expect(claude.value).toBe('gmail')
    expect(screen.getByRole('option', { name: 'Work — third@example.invalid (not logged in)' }).disabled).toBe(true)
    fireEvent.change(claude, { target: { value: 'anthropic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    await waitFor(() => expect(count(rest, '/switch')).toBe(1))
    expect(rest.calls.find(call => call.path === '/switch').opts.body).toEqual({ confirmed: true, claude: 'anthropic' })
  })

  it('posts both providers when both change', async () => {
    const rest = makeRest()
    await openDialog(rest)
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'work' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Claude account' }), { target: { value: 'gmail' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    await waitFor(() => expect(count(rest, '/switch')).toBe(1))
    expect(rest.calls.find(call => call.path === '/switch').opts.body).toEqual({ confirmed: true, codex: 'work', claude: 'gmail' })
  })

  it('shows an accepted POST as pending, not as success', async () => {
    const rest = makeRest()
    await openDialog(rest)
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'work' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    expect(await screen.findByText(/accepted, but it is still pending/i)).toBeTruthy()
    expect(screen.getByText(/The Hermes gateway and Hermes Desktop may close and reopen/i)).toBeTruthy()
    expect(screen.queryByText(/^Account switch complete\.$/i)).toBeNull()
    expect(count(rest, '/switch')).toBe(1)
  })

  it('disables confirmation when the backend reports a pending operation', async () => {
    const rest = makeRest({ status: [STATUS_PENDING], preflight: [preflightFor(STATUS_PENDING, STATUS_PENDING.blockers)] })
    await openDialog(rest)
    const confirm = screen.getByRole('button', { name: 'Pending…' })
    expect(confirm.disabled).toBe(true)
    fireEvent.click(confirm)
    expect(count(rest, '/switch')).toBe(0)
  })

  it('polls status every 10 seconds and never retries an accepted POST', async () => {
    const rest = makeRest({ status: [STATUS_PERSONAL_IDLE, STATUS_PERSONAL_IDLE, STATUS_PENDING] })
    const originalSetInterval = window.setInterval
    const scheduled = []
    window.setInterval = (callback, interval) => {
      scheduled.push({ callback, interval })
      return originalSetInterval(callback, interval)
    }
    try {
      await openDialog(rest)
      const poll = scheduled.find(item => item.interval === 10_000)
      expect(poll).toBeTruthy()
      fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'work' } })
      fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
      await waitFor(() => expect(count(rest, '/switch')).toBe(1))
      const before = count(rest, '/status')
      poll.callback()
      await waitFor(() => expect(count(rest, '/status')).toBe(before + 1))
      expect(count(rest, '/preflight')).toBe(1)
      expect(count(rest, '/switch')).toBe(1)
    } finally {
      window.setInterval = originalSetInterval
    }
  })

  it('uses complete and failed status operations as terminal outcomes', async () => {
    await openDialog(makeRest({ status: [STATUS_VERIFIED_Work] }))
    expect(screen.getByRole('status').textContent).toContain(STATUS_VERIFIED_Work.last_operation.message)
    cleanup()
    await openDialog(makeRest({ status: [STATUS_FAILED] }))
    expect(screen.getByRole('alert').textContent).toContain(STATUS_FAILED.last_operation.message)
  })

  it('shows a safe backend error detail when the switch request fails', async () => {
    const rest = makeRest({ switchResponse: async () => { throw { detail: SWITCH_ERROR_DETAIL } } })
    await openDialog(rest)
    fireEvent.change(screen.getByRole('combobox', { name: 'Target Codex account' }), { target: { value: 'work' } })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    expect((await screen.findByRole('alert')).textContent).toContain(SWITCH_ERROR_DETAIL)
    expect(screen.queryByText(/^Account switch complete\.$/i)).toBeNull()
  })

  it('fails closed on an incomplete pre-flight payload', async () => {
    const rest = makeRest({ preflight: [{ codex: STATUS_PERSONAL_IDLE.codex, claude: STATUS_PERSONAL_IDLE.claude }] })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: /Codex:/ }))
    await screen.findByRole('dialog')
    expect((await screen.findByRole('alert')).textContent).toMatch(/response was incomplete/i)
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
    expect(count(rest, '/switch')).toBe(0)
  })

  it('shows the backend sentence without the wrapping Hermes Desktop puts around a failed request', async () => {
    // Seen on a real install: the dialog showed the error class, the status code, braces and doubled backslashes.
    const sentence = 'Account-switch settings are missing. Run setup.cmd from the plugin\'s folder to create them, or copy settings.example.json to C:\\Users\\example-user\\settings.json and fill it in.'
    const rest = makeRest({ statusFailure: new Error(`Error invoking remote method 'hermes:api': Error: 409: ${JSON.stringify({ detail: sentence })}`) })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: 'Codex: Unknown · Claude: Unknown' }))
    expect((await screen.findByRole('alert')).textContent).toBe(sentence)
    expect(screen.getByRole('dialog').textContent).toMatch(/used by all Hermes profiles on this PC\./)
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
  })

  it('shows a failure that carries no backend answer without its wrapping', async () => {
    const rest = makeRest({ statusFailure: new Error("Error invoking remote method 'hermes:api': Error: connect ECONNREFUSED 127.0.0.1:1") })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: 'Codex: Unknown · Claude: Unknown' }))
    expect((await screen.findByRole('alert')).textContent).toBe('connect ECONNREFUSED 127.0.0.1:1')
  })

  it('makes a missing backend visible and fail closed', async () => {
    const rest = makeRest({ statusFailure: { status: 404, detail: 'Not found' } })
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    expect(await screen.findByRole('button', { name: 'Codex: Unknown · Claude: Unknown' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Codex: Unknown · Claude: Unknown' }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect((await screen.findByRole('alert')).textContent).toMatch(/404.*not be enabled for this profile.*Desktop needs one restart/i)
    expect(screen.getByRole('button', { name: 'Switch account' }).disabled).toBe(true)
    expect(count(rest, '/switch')).toBe(0)
  })
})

describe('first run: the dialog sets the plugin up', () => {
  const NOT_SET_UP = {
    setup_needed: true, codex: { selected: null, accounts: [], effective: null, warnings: [] },
    claude: { selected: null, state: 'unset', accounts: [] }, last_operation: null, in_progress: false, blockers: [],
    scope: 'All Hermes profiles on this PC'
  }
  const FOUND = {
    settings_present: false,
    settings_file: 'C:\\Users\\example-user\\AppData\\Local\\hermes\\plugin-data\\codex-account-switch\\settings.json',
    setup_command: 'C:\\Users\\example-user\\AppData\\Local\\hermes\\plugins\\codex-account-switch\\setup.cmd',
    gateway_service: 'FixtureGateway',
    name_rule: 'use up to 24 letters, digits and spaces',
    codex: [{ email: 'first@example.invalid', name: 'First', set_up: false }, { email: 'second@example.invalid', name: 'Second', set_up: false }],
    claude: [{ key: 'main', email: 'third@example.invalid', name: 'Main', verified: true, set_up: false }]
  }

  function setupRest({ found = FOUND, save = { ok: true, codex: 2, claude: 1 }, after = STATUS_PERSONAL_IDLE } = {}) {
    const calls = []
    let saved = false
    const rest = async (path, opts = {}) => {
      calls.push({ path, opts })
      if (path === '/status') return saved ? after : NOT_SET_UP
      if (path === '/setup' && opts.method === 'POST') {
        const answer = typeof save === 'function' ? await save(opts) : save
        saved = answer?.ok === true
        return answer
      }
      if (path === '/setup') {
        if (found instanceof Error) throw found
        return found
      }
      if (path === '/preflight') return preflightFor(after)
      throw new Error(`Unexpected fixture path: ${path}`)
    }
    rest.calls = calls
    return rest
  }

  async function openSetup(rest) {
    const { statusContribution } = registerWith(rest)
    render(statusContribution.render())
    fireEvent.click(await screen.findByRole('button', { name: 'Account switch: set up' }))
    await screen.findByRole('dialog')
  }

  it('shows the accounts found on this PC, takes a name for each and saves them', async () => {
    const rest = setupRest()
    await openSetup(rest)
    expect(screen.getByRole('heading', { name: 'Set up account switching' })).toBeTruthy()
    const first = await screen.findByRole('textbox', { name: 'Name for first@example.invalid' })
    expect(first.value).toBe('First')
    expect(screen.getByRole('dialog').textContent).toMatch(/Gateway: Windows service FixtureGateway/)
    expect(count(rest, '/preflight')).toBe(0)
    fireEvent.change(first, { target: { value: '  Home   PC ' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Name for third@example.invalid' }), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(rest.calls.filter(call => call.path === '/setup' && call.opts.method === 'POST').length).toBe(1))
    expect(rest.calls.find(call => call.opts.method === 'POST').opts.body).toEqual({
      confirmed: true,
      codex: { 'first@example.invalid': 'Home PC', 'second@example.invalid': 'Second' },
      claude: { main: 'Main' }
    })
    // Set up: the same button now opens the switch dialog, after the idle check.
    expect(await screen.findByRole('combobox', { name: 'Target Codex account' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Codex: Personal/ })).toBeTruthy()
    expect(count(rest, '/preflight')).toBe(1)
  })

  it('does not save a name that cannot be shown', async () => {
    const rest = setupRest()
    await openSetup(rest)
    const first = await screen.findByRole('textbox', { name: 'Name for first@example.invalid' })
    fireEvent.change(first, { target: { value: '.\\setup.cmd claude' } })
    expect(first.getAttribute('aria-invalid')).toBe('true')
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true)
    fireEvent.change(first, { target: { value: 'Work' } })
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(false)
    expect(rest.calls.some(call => call.opts.method === 'POST')).toBe(false)
  })

  it('shows why a save was refused and stays on the setup', async () => {
    const rest = setupRest({ save: async () => { throw new Error("Error invoking remote method 'hermes:api': Error: 409: {\"detail\":\"An account switch is already in progress.\"}") } })
    await openSetup(rest)
    await screen.findByRole('textbox', { name: 'Name for first@example.invalid' })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByRole('alert')).textContent).toBe('An account switch is already in progress.')
    expect(screen.getByRole('heading', { name: 'Set up account switching' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(false)
  })

  it('hands an agent instructions that name the real command and leave switching and sign-in to the person', async () => {
    const rest = setupRest()
    await openSetup(rest)
    await screen.findByRole('textbox', { name: 'Name for first@example.invalid' })
    fireEvent.click(screen.getByRole('button', { name: 'Copy instructions for my agent' }))
    const text = (await screen.findByRole('textbox', { name: 'Instructions for your agent' })).value
    expect(text).toContain(FOUND.setup_command)
    expect(text).toMatch(/word show after it/)
    expect(text).toMatch(/auto "me@example.com=Personal" "claude:me@example.com=Family"/)
    expect(text).toMatch(/You cannot sign in for me/)
    expect(text).toMatch(/Do not switch accounts/)
    expect(text).not.toMatch(/first@example\.invalid/)
  })

  it('says what is missing when no account is signed in, and offers nothing to save', async () => {
    const rest = setupRest({ found: { ...FOUND, gateway_service: null, codex: [], claude: [] } })
    await openSetup(rest)
    expect(await screen.findByText(/No Codex account is signed in to Hermes/)).toBeTruthy()
    expect(screen.getByRole('dialog').textContent).toMatch(/Gateway: started by Hermes itself/)
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true)
  })

  it('says so when there is only one account, and lists an account that is set up already without asking', async () => {
    const rest = setupRest({ found: { ...FOUND, codex: [{ email: 'first@example.invalid', name: 'First', set_up: true }], claude: [] } })
    await openSetup(rest)
    expect(await screen.findByText('Already set up.')).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: 'Name for first@example.invalid' })).toBeNull()
    expect(screen.getByRole('status').textContent).toMatch(/nothing to switch between yet/)
  })

  it('shows a plain reason when this PC cannot be read, and looks again on request', async () => {
    const rest = setupRest({ found: new Error("Error invoking remote method 'hermes:api': Error: 409: {\"detail\":\"The Hermes login store could not be read.\"}") })
    await openSetup(rest)
    expect((await screen.findByRole('alert')).textContent).toBe('The Hermes login store could not be read.')
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Look again' }))
    await waitFor(() => expect(count(rest, '/setup')).toBe(2))
  })
})
