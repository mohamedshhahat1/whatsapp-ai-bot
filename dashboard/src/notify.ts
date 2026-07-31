// Attention signals for events an operator must not miss.
//
// A sales lead that arrives while the operator is looking at another screen
// used to be completely silent: there was no toast, no sound and no title
// change anywhere in the dashboard. Ordering alone was the only signal, and
// it is invisible unless you happen to be watching the list.

type AudioContextCtor = typeof AudioContext

let audioContext: AudioContext | null = null

// Synthesised rather than shipped as an asset: a two-note chime is a few
// lines of WebAudio and avoids adding a binary to the bundle.
export function playChime(): void {
  try {
    const ctor: AudioContextCtor | undefined =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: AudioContextCtor })
        .webkitAudioContext
    if (!ctor) return

    const context = audioContext ?? new ctor()
    audioContext = context
    // Browsers keep the context suspended until the page has been interacted
    // with. Resuming is a no-op when it is already running.
    if (context.state === "suspended") void context.resume()

    const now = context.currentTime
    const gain = context.createGain()
    gain.gain.setValueAtTime(0.0001, now)
    gain.gain.exponentialRampToValueAtTime(0.1, now + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.55)
    gain.connect(context.destination)

    const notes = [880, 1320]
    notes.forEach((frequency, index) => {
      const start = now + index * 0.12
      const oscillator = context.createOscillator()
      oscillator.type = "sine"
      oscillator.frequency.setValueAtTime(frequency, start)
      oscillator.connect(gain)
      oscillator.start(start)
      oscillator.stop(start + 0.3)
    })
  } catch {
    // Audio is a nicety. Never let it break the caller's event handler.
  }
}

const BASE_TITLE = document.title
let flashTimer = 0

// Flashes the tab title so a lead is visible when the dashboard is in a
// background tab, which is where it usually is.
export function flashTitle(message: string, times = 6): void {
  window.clearInterval(flashTimer)
  let count = 0
  flashTimer = window.setInterval(() => {
    document.title = count % 2 === 0 ? message : BASE_TITLE
    count += 1
    if (count > times) {
      window.clearInterval(flashTimer)
      document.title = BASE_TITLE
    }
  }, 900)
}

export function resetTitle(): void {
  window.clearInterval(flashTimer)
  document.title = BASE_TITLE
}
