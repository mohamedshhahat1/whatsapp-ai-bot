import { useState } from "react"
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import { api } from "../api"
import { Card, Empty, Loader, Refreshing, useAsync } from "../components/Async"
import { datetime, money, ms, number, percent } from "../format"

const RANGES = [7, 30, 90]

// Costs move slowly and every poll is three admin queries, so 60s is enough
// to feel live without adding load.
const POLL_MS = 60_000

export default function Overview() {
  const [days, setDays] = useState(30)

  const overview = useAsync(() => api.overview(days), [days], POLL_MS)
  const daily = useAsync(() => api.daily(days), [days], POLL_MS)
  const questions = useAsync(() => api.questions(days, 10), [days], POLL_MS)

  return (
    <>
      <div className="page-header">
        <h1>Overview</h1>
        <div className="row">
          <Refreshing active={overview.refreshing} />
          {RANGES.map((range) => (
            <button
              key={range}
              className={days === range ? "primary" : ""}
              onClick={() => setDays(range)}
            >
              {range}d
            </button>
          ))}
        </div>
      </div>

      <Loader loading={overview.loading} error={overview.error}>
        {overview.data && (
          <div className="cards">
            <Card
              label="OpenAI spend"
              value={money(overview.data.cost.total_cost_usd)}
              hint={`last ${overview.data.period_days} days`}
            />
            <Card
              label="Projected monthly"
              value={money(overview.data.projected_monthly_cost_usd)}
              hint="at the current rate"
            />
            <Card
              label="Total tokens"
              value={number(overview.data.cost.total_tokens)}
              hint={`${number(overview.data.cost.prompt_tokens)} in / ${number(
                overview.data.cost.completion_tokens,
              )} out`}
            />
            {/* Sessions, not customers. Since conversations close on idle,
                this denominator is much larger and more volatile than it was
                when a conversation lasted a customer's lifetime. */}
            <Card
              label="Cost per session"
              value={money(overview.data.cost_per_conversation_usd)}
              hint={`${number(
                overview.data.active_conversations,
              )} sessions active in period`}
            />
            <Card
              label="Avg response time"
              value={ms(overview.data.avg_latency_ms)}
              hint={`p95 ${ms(overview.data.p95_latency_ms)}`}
            />
            <Card
              label="Customers"
              value={number(overview.data.total_users)}
              hint={`${number(overview.data.new_users)} new in period`}
            />
            {/* Renamed from "Conversations". One customer now produces many
                of these over time, so reading it as a headcount overstates
                reach badly. The hint says so rather than leaving it to be
                inferred from a number that looks too big. */}
            <Card
              label="Sessions"
              value={number(overview.data.total_conversations)}
              hint={`${number(
                overview.data.new_conversations,
              )} new in period \u00b7 several per customer`}
            />
            <Card
              label="Messages in period"
              value={number(overview.data.messages_in_period)}
              hint={`${number(overview.data.total_messages)} all time`}
            />
            <Card
              label="AI requests"
              value={number(overview.data.ai_requests)}
              hint={`${number(overview.data.ai_errors)} failed`}
            />
            <Card
              label="Error rate"
              value={percent(overview.data.error_rate)}
              hint={`since ${datetime(overview.data.since)}`}
            />
          </div>
        )}
      </Loader>

      <div className="panel">
        <h2>Daily usage and cost</h2>
        <Loader loading={daily.loading} error={daily.error}>
          {daily.data && daily.data.length === 0 && (
            <Empty>No activity in this period yet.</Empty>
          )}
          {daily.data && daily.data.length > 0 && (
            <ResponsiveContainer width="100%" height={280}>
              <ComposedChart data={daily.data}>
                <CartesianGrid stroke="#272c37" vertical={false} />
                <XAxis dataKey="day" stroke="#939aab" fontSize={12} />
                <YAxis yAxisId="left" stroke="#939aab" fontSize={12} />
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  stroke="#939aab"
                  fontSize={12}
                />
                <Tooltip
                  contentStyle={{
                    background: "#1e222b",
                    border: "1px solid #272c37",
                    borderRadius: 8,
                  }}
                />
                <Legend />
                <Bar
                  yAxisId="left"
                  dataKey="messages"
                  name="Messages"
                  fill="#25d366"
                  radius={[4, 4, 0, 0]}
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="cost_usd"
                  name="Cost (USD)"
                  stroke="#f2c14e"
                  dot={false}
                  strokeWidth={2}
                />
              </ComposedChart>
            </ResponsiveContainer>
          )}
        </Loader>
      </div>

      <div className="panel">
        <h2>Model requests and latency</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Messages and requests diverge when the bot is off, when customers are
          rate limited, or when a conversation is owned by a human.
        </p>
        <Loader loading={daily.loading} error={daily.error}>
          {daily.data && daily.data.length === 0 && (
            <Empty>No activity in this period yet.</Empty>
          )}
          {daily.data && daily.data.length > 0 && (
            <ResponsiveContainer width="100%" height={240}>
              <ComposedChart data={daily.data}>
                <CartesianGrid stroke="#272c37" vertical={false} />
                <XAxis dataKey="day" stroke="#939aab" fontSize={12} />
                <YAxis yAxisId="left" stroke="#939aab" fontSize={12} />
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  stroke="#939aab"
                  fontSize={12}
                />
                <Tooltip
                  contentStyle={{
                    background: "#1e222b",
                    border: "1px solid #272c37",
                    borderRadius: 8,
                  }}
                />
                <Legend />
                <Bar
                  yAxisId="left"
                  dataKey="requests"
                  name="AI requests"
                  fill="#1a9e4b"
                  radius={[4, 4, 0, 0]}
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="avg_latency_ms"
                  name="Avg latency (ms)"
                  stroke="#939aab"
                  dot={false}
                  strokeWidth={2}
                />
              </ComposedChart>
            </ResponsiveContainer>
          )}
        </Loader>
      </div>

      <div className="panel">
        <h2>Daily token breakdown</h2>
        <Loader loading={daily.loading} error={daily.error}>
          {daily.data && daily.data.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Day</th>
                    <th>Requests</th>
                    <th>Prompt tokens</th>
                    <th>Completion tokens</th>
                    <th>Total tokens</th>
                    <th>Avg latency</th>
                    <th>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {daily.data.map((row) => (
                    <tr key={row.day}>
                      <td>{row.day}</td>
                      <td>{number(row.requests)}</td>
                      <td>{number(row.prompt_tokens)}</td>
                      <td>{number(row.completion_tokens)}</td>
                      <td>{number(row.total_tokens)}</td>
                      <td className="muted">{ms(row.avg_latency_ms)}</td>
                      <td>{money(row.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {daily.data && daily.data.length === 0 && (
            <Empty>No activity in this period yet.</Empty>
          )}
        </Loader>
      </div>

      <div className="panel">
        <h2>Most frequently asked questions</h2>
        <Loader loading={questions.loading} error={questions.error}>
          {questions.data && questions.data.length === 0 && (
            <Empty>Not enough messages yet.</Empty>
          )}
          {questions.data && questions.data.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Question</th>
                    <th style={{ width: 90 }}>Times</th>
                    <th style={{ width: 190 }}>Last asked</th>
                  </tr>
                </thead>
                <tbody>
                  {questions.data.map((question) => (
                    <tr key={question.question}>
                      <td>{question.question}</td>
                      <td>{question.count}</td>
                      <td className="muted">{datetime(question.last_asked)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Loader>
      </div>
    </>
  )
}
