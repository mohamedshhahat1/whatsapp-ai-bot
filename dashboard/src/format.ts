export function money(value: number): string {
  // Per-conversation costs are fractions of a cent, so the usual 2 decimals
  // would render everything as $0.00.
  if (value === 0) return "$0"
  if (value < 0.01) return "$" + value.toFixed(5)
  return "$" + value.toFixed(2)
}

export function number(value: number): string {
  return value.toLocaleString("en-US")
}

export function ms(value: number): string {
  if (value >= 1000) return (value / 1000).toFixed(2) + " s"
  return Math.round(value) + " ms"
}

export function datetime(value: string | null): string {
  if (!value) return "-"
  return new Date(value).toLocaleString()
}

// Date without a time, for price periods where the time of day is noise.
export function date(value: string | null): string {
  if (!value) return "-"
  return new Date(value).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  })
}

export function percent(value: number): string {
  return (value * 100).toFixed(1) + "%"
}

export interface ChannelDisplay {
  icon: string
  label: string
}

// The icons are the ones app/channels/constants.py already records in
// ChannelProfile.icon. Repeating them here rather than serving them keeps a
// glyph out of the API payload, but it does mean the two lists have to be
// changed together -- that module is append-only, so this is a rare event.
const CHANNELS: Record<string, ChannelDisplay> = {
  whatsapp: { icon: "\u{1F7E2}", label: "WhatsApp" },
  messenger: { icon: "\u{1F535}", label: "Messenger" },
  instagram_dm: { icon: "\u{1F7E3}", label: "Instagram" },
  facebook_comment: { icon: "\u{1F537}", label: "FB comment" },
  instagram_comment: { icon: "\u{1F49C}", label: "IG comment" },
}

// Never throws, and treats a missing value as WhatsApp.
//
// Both cases are real rather than defensive padding. `undefined` is what an
// older backend sends during a rolling deploy, and every conversation from
// before channels existed was WhatsApp. An unrecognised string is a NEWER
// backend talking to this build; it gets a neutral glyph and its raw name,
// which tells an operator more than hiding the row would.
export function channelDisplay(value: string | null | undefined): ChannelDisplay {
  if (!value) return CHANNELS.whatsapp
  return CHANNELS[value] ?? { icon: "\u{1F4AC}", label: value }
}
