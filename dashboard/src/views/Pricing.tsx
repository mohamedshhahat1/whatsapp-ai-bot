import { FormEvent, useEffect, useState } from "react"

import { ApiError, ModelCost, ModelPricing, api } from "../api"

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  })
}

function money(value: number): string {
  return "$" + value.toFixed(value < 1 ? 4 : 2)
}

export default function Pricing() {
  const [rows, setRows] = useState<ModelPricing[]>([])
  const [models, setModels] = useState<ModelCost[]>([])
  const [error, setError] = useState("")
  const [saving, setSaving] = useState(false)

  const [model, setModel] = useState("")
  const [input, setInput] = useState("")
  const [output, setOutput] = useState("")
  const [from, setFrom] = useState("")
  const [note, setNote] = useState("")

  async function load() {
    try {
      const [pricing, breakdown] = await Promise.all([
        api.pricing(),
        api.models(30),
      ])
      setRows(pricing)
      setModels(breakdown)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load pricing")
    }
  }

  useEffect(() => {
    void load()
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!model.trim() || !input || !output) return
    setSaving(true)
    setError("")
    try {
      await api.addPricing({
        model: model.trim(),
        input_price_per_1m: Number(input),
        output_price_per_1m: Number(output),
        // A date input gives a plain date; send it as UTC midnight.
        effective_from: from ? new Date(from).toISOString() : undefined,
        note: note.trim() || undefined,
      })
      setModel("")
      setInput("")
      setOutput("")
      setFrom("")
      setNote("")
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save price")
    } finally {
      setSaving(false)
    }
  }

  async function remove(id: number) {
    if (
      !window.confirm(
        "Deleting a price period changes the cost of every call inside it. " +
          "Continue?",
      )
    ) {
      return
    }
    try {
      await api.deletePricing(id)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to delete")
    }
  }

  // Group by model so each model's history reads as a timeline.
  const byModel = new Map<string, ModelPricing[]>()
  for (const row of rows) {
    const list = byModel.get(row.model) ?? []
    list.push(row)
    byModel.set(row.model, list)
  }

  return (
    <div>
      <header className="page-header">
        <h1>Model pricing</h1>
        <p className="muted">
          Every AI call is costed with the price that was in force when it was
          made. Adding a new price never changes past figures.
        </p>
      </header>

      {error && <div className="panel error">{error}</div>}

      <div className="panel">
        <h2>Add a price change</h2>
        <form className="pricing-form" onSubmit={submit}>
          <label>
            Model
            <input
              value={model}
              placeholder="gpt-4.1-mini"
              onChange={(e) => setModel(e.target.value)}
            />
          </label>
          <label>
            Input $ / 1M
            <input
              type="number"
              step="0.000001"
              min="0"
              value={input}
              placeholder="0.40"
              onChange={(e) => setInput(e.target.value)}
            />
          </label>
          <label>
            Output $ / 1M
            <input
              type="number"
              step="0.000001"
              min="0"
              value={output}
              placeholder="1.60"
              onChange={(e) => setOutput(e.target.value)}
            />
          </label>
          <label>
            Effective from
            <input
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </label>
          <label className="wide">
            Note
            <input
              value={note}
              placeholder="Why this changed"
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <button className="primary" disabled={saving}>
            {saving ? "Saving..." : "Add price"}
          </button>
        </form>
        <p className="muted" style={{ fontSize: 12 }}>
          Leave the date empty to apply from now. Backdate it to fill in
          history you already know.
        </p>
      </div>

      <div className="panel">
        <h2>Price history</h2>
        {byModel.size === 0 && <p className="muted">No prices recorded yet.</p>}
        {[...byModel.entries()].map(([name, history]) => (
          <div key={name} className="pricing-group">
            <h3>{name}</h3>
            <table className="table">
              <thead>
                <tr>
                  <th>Effective from</th>
                  <th>Input / 1M</th>
                  <th>Output / 1M</th>
                  <th>Note</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {history.map((row, index) => (
                  <tr key={row.id}>
                    <td>
                      {formatDate(row.effective_from)}
                      {index === 0 && <span className="badge">current</span>}
                    </td>
                    <td>{money(row.input_price_per_1m)}</td>
                    <td>{money(row.output_price_per_1m)}</td>
                    <td className="muted">{row.note ?? ""}</td>
                    <td>
                      <button
                        className="link danger"
                        onClick={() => void remove(row.id)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>

      <div className="panel">
        <h2>Spend by model, last 30 days</h2>
        {models.length === 0 && <p className="muted">No AI calls recorded.</p>}
        {models.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Calls</th>
                <th>Input tokens</th>
                <th>Output tokens</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {models.map((row) => (
                <tr key={row.model}>
                  <td>{row.model}</td>
                  <td>{row.requests.toLocaleString()}</td>
                  <td>{row.prompt_tokens.toLocaleString()}</td>
                  <td>{row.completion_tokens.toLocaleString()}</td>
                  <td>{money(row.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
