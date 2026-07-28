import { api } from "../api"
import { Loader, useAsync } from "../components/Async"
import { datetime, number } from "../format"

export default function Customers() {
  const customers = useAsync(() => api.customers(100), [])

  return (
    <>
      <div className="page-header">
        <h1>Customers</h1>
        <button onClick={customers.reload}>Refresh</button>
      </div>

      <div className="panel">
        <Loader loading={customers.loading} error={customers.error}>
          {customers.data && customers.data.length === 0 && (
            <p className="muted">No customers yet.</p>
          )}
          {customers.data && customers.data.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>WhatsApp number</th>
                  <th>Conversations</th>
                  <th>Messages</th>
                  <th>Last active</th>
                </tr>
              </thead>
              <tbody>
                {customers.data.map((customer) => (
                  <tr key={customer.user_id}>
                    <td>{customer.name || <span className="muted">-</span>}</td>
                    <td>{customer.wa_id}</td>
                    <td>{number(customer.conversations)}</td>
                    <td>{number(customer.messages)}</td>
                    <td className="muted">{datetime(customer.last_active)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Loader>
      </div>
    </>
  )
}
