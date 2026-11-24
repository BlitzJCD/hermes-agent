/** Ephemeral fixed-schema scalar diagnostics. No audio, IDs, labels or exception text. */
const COUNTERS = [
  'capture_open_calls', 'capture_open_errors', 'graph_started', 'audio_callbacks',
  'pcm_frames_generated', 'silent_frames_skipped', 'frames_queued', 'queue_dropped',
  'frames_stop_dropped', 'feed_calls_sent', 'feed_frames_sent', 'feed_ack_ok',
  'feed_ack_refused', 'feed_ack_unknown', 'feed_rpc_errors', 'feed_frames_acknowledged',
  'feed_frames_refused', 'capture_failures', 'capture_stops', 'event_received',
  'handoff_requested', 'handoff_consumed', 'voice_capture_started', 'voice_start_errors',
  'late_events_dropped', 'late_handoffs_dropped'
] as const
export type WakeCounter = typeof COUNTERS[number]
type Health = { track_live: number; track_muted: number; track_enabled: number; context_state: string }
const CONTEXT_STATES = ['unknown', 'running', 'suspended', 'interrupted', 'closed']
export class WakeTrace {
  private counts = Object.fromEntries(COUNTERS.map(k => [k, 0])) as Record<WakeCounter, number>
  private health: Health = { track_live: 0, track_muted: 0, track_enabled: 0, context_state: 'unknown' }
  bump(key: WakeCounter, amount = 1): void {
    if (!COUNTERS.includes(key) || !Number.isSafeInteger(amount) || amount < 0) throw new Error('invalid diagnostic counter')
    this.counts[key] = Math.min(2147483647, this.counts[key] + amount)
  }
  captureHealth(tracks: readonly Pick<MediaStreamTrack, 'readyState' | 'muted' | 'enabled'>[], context: string): void {
    this.health = {
      track_live: Math.min(2147483647, tracks.filter(t => t.readyState === 'live').length),
      track_muted: Math.min(2147483647, tracks.filter(t => t.muted).length),
      track_enabled: Math.min(2147483647, tracks.filter(t => t.enabled).length),
      context_state: CONTEXT_STATES.includes(context) ? context : 'unknown'
    }
  }
  snapshot(): Record<string, number | string> { return { ...this.counts, ...this.health } }
}
export const wakeTrace = new WakeTrace()

/** Reject future unrecognized server fields rather than printing arbitrary payloads. */
export function safeGatewayWakeDiagnostics(value: unknown): Record<string, number | string | boolean> {
  if (!value || typeof value !== 'object') return {}
  const result: Record<string, number | string | boolean> = {}
  const allowed = [
    'start_calls', 'pause_calls', 'resume_calls', 'stop_calls', 'failures',
    'feed_calls', 'feed_samples', 'queue_enqueued', 'queue_dropped', 'queue_consumed',
    'queue_stale_dropped', 'queue_stop_dropped', 'engine_calls', 'engine_hits',
    'engine_no_hit_chunks', 'engine_errors', 'fire_errors', 'feed_errors',
    'callback_dispatched', 'callback_errors', 'cooldown_suppressed', 'event_emitted', 'event_errors',
    'event_attempts', 'event_refused', 'event_unknown'
  ]
  for (const key of allowed) {
    const v = (value as Record<string, unknown>)[key]
    if (typeof v === 'number' && Number.isSafeInteger(v) && v >= 0) result[key] = Math.min(2147483647, v)
  }
  const state = (value as Record<string, unknown>).state
  if (typeof state === 'string' && ['idle', 'starting', 'listening', 'paused', 'stopped', 'failed'].includes(state)) result.state = state
  return result
}
