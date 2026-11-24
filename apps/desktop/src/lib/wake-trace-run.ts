/** Explicit /wake trace only. No background listener or timer during normal use. */
let active = false
let stopping = false
let blocked = false
export const wakeTraceBlocked = (): boolean => blocked
/** Only a new explicit owner action may release the passive-activation fence. */
export const allowWakeTraceActivation = (): void => { blocked = false }
const endHandlers = new Set<() => void>()
export const wakeTraceStopping = (): boolean => stopping
export function onWakeTraceEnd(handler: () => void): () => void {
  endHandlers.add(handler)
  return () => { endHandlers.delete(handler) }
}
export async function runBoundedWakeTrace(hooks: {
  start: () => Promise<void>
  stopRemote: () => Promise<unknown>
  stopCapture: () => void
}): Promise<void> {
  if (active) throw new Error('wake diagnostic already running')
  active = true
  blocked = false
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    // Native start RPC is separately capped at 10s; this capture window is 20s.
    await hooks.start()
    await new Promise<void>(resolve => { timer = setTimeout(resolve, 20_000) })
  } finally {
    blocked = true
    stopping = true
    if (timer !== undefined) clearTimeout(timer)
    try {
      // Enqueue server shutdown first, but never wait for it to stop browser PCM.
      let remote: Promise<unknown> | undefined
      try { remote = hooks.stopRemote() } finally {
        hooks.stopCapture()
        for (const end of endHandlers) { try { end() } catch { /* continue remaining shutdowns */ } }
      }
      await remote
    } finally {
      active = false
      stopping = false
    }
  }
}
