import { createElement } from 'react'

export function cn(...values) {
  return values.filter(Boolean).join(' ')
}

export function haptic() {}

// Tests set globalThis.__activeConnectionId to stand in for the active chat's connection.
export const host = {
  activeConnectionId: () => globalThis.__activeConnectionId ?? null
}

export function Button({ children, variant: _variant, size: _size, asChild: _asChild, ...props }) {
  return createElement('button', { type: 'button', ...props }, children)
}

export function Dialog({ open, children }) {
  return open ? createElement('div', { 'data-test-dialog-root': 'true' }, children) : null
}

export function DialogContent({ children, ...props }) {
  return createElement('div', { role: 'dialog', 'aria-modal': 'true', ...props }, children)
}

export function DialogDescription({ children, ...props }) {
  return createElement('p', props, children)
}

export function DialogFooter({ children, ...props }) {
  return createElement('div', props, children)
}

export function DialogHeader({ children, ...props }) {
  return createElement('div', props, children)
}

export function DialogTitle({ children, ...props }) {
  return createElement('h2', props, children)
}
