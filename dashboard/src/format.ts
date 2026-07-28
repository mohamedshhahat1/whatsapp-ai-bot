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

export function percent(value: number): string {
  return (value * 100).toFixed(1) + "%"
}
