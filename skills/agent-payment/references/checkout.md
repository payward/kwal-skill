# Checkout and payment status

## Review and submit

```bash
python3 scripts/register.py quote-check "<quote-id>"
python3 scripts/register.py checkout "<quote-id>"
```

Replace placeholders before execution. Run `quote-check` first. Confirm the items, quantities, delivery details, final total, currency, expiry, and funding readiness. Use the user's existing authorization if it covers that exact purchase and total. Otherwise present the review and ask for approval before `checkout`.

`checkout` submits immediately. Its printed quote review is not an approval pause. It refuses an expired quote or a quote with a missing shipping choice. For those states, return to [Quotes](quotes.md) before submission.

The command saves the payment id and quote id before it sends the request. A repeated `checkout` for the same quote uses the saved payment id. If the response is lost, repeat that command under the [retry limit](debug.md#retries), with the same credentials and attempt record. `Checkout: resuming the recorded attempt` confirms reuse of the saved id.

Payment history belongs to the credentials file: `alice.json` uses
`alice.json.payments.json` beside it. Keep both files together when moving a
session. Concurrent commands reuse one saved payment id for the same quote.

## Observe the same payment

```bash
python3 scripts/register.py payment
python3 scripts/register.py payment "<payment-id>"
```

`payment` reads the last recorded attempt. Supply an id to read a different payment. Use these commands after a restart. Report the printed identifiers, state, and amounts.

| Output | Action and completion criterion |
| --- | --- |
| `Payment: waiting for the human approval` | Give the user the hosted approval link and expiry. The user approves and completes provider challenges on that page. Then read the same payment again. Card details stay on the hosted page. |
| `Approval link: expired` | The link expiry does not determine the payment outcome. Read the same payment again. If it still needs an expired link, ask the operator to resolve that attempt. Resolve the original outcome before considering a replacement. |
| `Payment: processing` | Follow the [processing limit](debug.md#processing-states). Continue with the same payment id. |
| `Payment: completed` | Report order completion and the order id and charged amount when supplied. Stop polling this payment. This response does not verify settlement. A missing card transaction or collection link does not delay order completion. |
| `Payment: declined` | Report the reason. A replacement needs a fresh quote and user authorization. |
| `Payment: stopped for operator help` | Report the step and reason. Ask the operator to resolve the same outcome. Keep the existing payment id. |
| `Payment: none recorded` | Ask for the payment id when a prior submission may exist. Start a new checkout only when no prior attempt needs resolution and the user authorizes the purchase. |

An unresolved payment remains the active attempt. A changed link, restart, timeout, or missing local record does not authorize a second purchase.

## Sandbox payments are simulated card spends

In the sandbox, `checkout` does not buy from a shop. No shop receives an order and nothing ships. `checkout` saves the payment. The payment worker then makes a simulated spend on your own sandbox card, and Kwal approves or declines it against the funds in your vault. The output says `Sandbox: this payment is a simulated card spend against the vault.` A payment read shows only saved results. It sends nothing.

| Output | Meaning |
| --- | --- |
| `Step: card_authorization` with `payment accepted; waiting for the payment worker` | Kwal saved the payment. The payment worker did not send it yet. Read the same payment again later. Do not start a new checkout. |
| `Step: card_authorization` with `another payment is using your card` | Another payment holds your card. Nothing was saved for this payment. Wait until the other payment finishes, then run the same `checkout` again. |
| `Step: card_authorization` with `Held on the vault` | Approved. The amount is held on your vault. Read the same payment again to see it settle. |
| `Step: card_clearing` with `Payment: processing` | The spend is settling. Read the same payment again. |
| `Payment: processing` with `ask the operator` in the reason | The results conflict, the vault collection is blocked, or the clearing is above the quote total. Report the reason. Ask the operator to resolve the same payment. Do not start a new checkout. |
| `Payment: completed` with `Settlement: the vault paid the simulated card spend` | Done. `Charged` is what the vault paid, in USDC. |
| `Payment: declined` | Kwal declined the spend; the reason names why. A new attempt needs a new quote. |
| `the vault cannot cover this quote` on `checkout` | Nothing was sent. Run `quote-check` for the quote to see the shortfall, fund the vault ([Funding](funding.md)), then run the same `checkout` again. |
| `Step: quote_already_used` or `Attached payment` on a new quote | Another payment already holds this quote. Reap returns the same quote for the same item and quantity for about 15 minutes, so a repeat purchase of the same basket reaches the paid quote. Choose another item or quantity, or wait and create a new quote. |
| `run the same checkout again` in the reason | The sandbox did not take the spend, or its outcome is unknown. Run the same `checkout` command; it reuses the saved payment id and never pays twice. |

`Card transaction id` names the sandbox card transaction. There is no approval link and no card detail to handle.

## Errors

- An unreadable or unwritable payment record: report it and ask for the last payment id. Observe that payment before another submission.
- An invalid payment response: follow [Debug](debug.md).
