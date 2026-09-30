"""Reversible admission gate for the actual Desktop RPC process, not a polling guess."""
import threading
import time
from compat import gate_target, has_active_delegations, server as gateway_server


# Requests that only READ for the screen: the project tree, session lists, usage meters, setup state.
# Hermes Desktop re-issues some of them all the time (the project tree for a large folder on OneDrive
# takes seconds each time), so one is nearly always in flight. Closing Desktop under one of these loses
# nothing: they start no agent work, and the screen they answer is about to close anyway.
HARMLESS = ('projects.', 'session.list', 'session.active_list', 'session.foreign.list', 'session.foreign.preview',
            'setup.', 'usage.', 'billing.state', 'subscription.state', 'subscription.preview', 'profiles.list',
            'profiles.describe', 'profiles.get_asset', 'model.options', 'complete.', 'learning.frames',
            'process.list', 'wake.status')


def harmless(method):
    return any(method == item or (item.endswith('.') and method.startswith(item)) for item in HARMLESS)


class DesktopGate:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.inflight = 0
        # method name -> [count, monotonic start of the oldest still running]: a refusal names what is running.
        self.running = {}
        self.until = 0.0
        # Long-running methods (shell.exec, slash.exec, llm.oneshot, ...) skip handle_request and enter
        # through _handle_admitted_request on a worker thread, so gate the function both paths share.
        self.target = gate_target(server)
        self.original = getattr(server, self.target)

    def dispatch(self, request):
        method = str((request or {}).get('method') or 'request') if isinstance(request, dict) else 'request'
        with self.lock:
            if time.monotonic() < self.until:
                return self.server._err(request.get('id'), 5038,
                    'Codex account switch is restarting Hermes. Please retry after reopening.')
            self.inflight += 1
            entry = self.running.setdefault(method, [0, time.monotonic()])
            entry[0] += 1
        try:
            return self.original(request)
        finally:
            with self.lock:
                self.inflight -= 1
                entry = self.running.get(method)
                if entry is not None:
                    entry[0] -= 1
                    if entry[0] <= 0:
                        del self.running[method]

    def busy(self):
        reasons = []
        blocking = {method: entry for method, entry in self.running.items() if not harmless(method)}
        if blocking or (self.inflight and not self.running):
            now = time.monotonic()
            names = ', '.join(f'{method} for {int(now - started)} s' for method, (count, started)
                              in sorted(blocking.items(), key=lambda item: item[1][1]))
            reasons.append(f'Desktop is handling a request ({names}).' if names else 'Desktop is handling a request.')
        s = self.server
        with s._sessions_lock:
            for sid, session in list(s._sessions.items()):
                live_thread = any(t is not None and t.is_alive()
                                  for t in (session.get('_run_thread'), session.get('_agent_build_thread')))
                if session.get('running') or live_thread or s._session_pending_kind(sid):
                    reasons.append('A conversation is running, initializing, or waiting for input.')
                elif has_active_delegations(sid, session):
                    reasons.append('A conversation owns running background work.')
        return sorted(set(reasons))

    def freeze(self):
        with self.lock:
            reasons = self.busy()
            if reasons:
                return {'ok': False, 'blockers': reasons}
            self.until = time.monotonic() + 180
            return {'ok': True, 'blockers': []}

    def release(self):
        with self.lock:
            self.until = 0

    def installed(self):
        return getattr(self.server, self.target, None) == self.dispatch


def install():
    server = gateway_server()
    prior = getattr(server, '_codex_account_switch_gate', None)
    if prior is not None:
        return prior
    gate = DesktopGate(server)
    setattr(server, gate.target, gate.dispatch)
    server._codex_account_switch_gate = gate
    return gate
