# Funding and readiness

```bash
python3 scripts/register.py funding
python3 scripts/register.py funding --required-minor-units "<minor-units>"
```

Replace placeholders before execution. `--required-minor-units` is the verified purchase funding amount in USDC with six decimals: `1500000` means 1.500000 USDC. It must fit an unsigned 64-bit integer. Omitting the amount, or passing zero, checks the balance only.

For a purchase, use `quote-check "<quote-id>"`. The current UAT helper converts USD totals to six-decimal USDC at 1 USD = 1 USDC and checks that amount. Other currencies, inexact conversions, and amounts outside the supported range are refused. Do not copy raw quote minor units into the funding command or invent another conversion.

Report the `Card-spendable` amount, echoed required amount, and shortfall that the command prints. An absent card-spendable amount means it was not read, not zero. The service owns the accounting; use its amounts. `Withdrawable: unknown (operator required)` means this API does not report an owner withdrawal amount. Follow [Owner withdrawal](vault-and-funds.md#owner-withdrawal) for withdrawal requests.

The helper also shows returned chain ID, token address, precision and faucet links. It shows transfer instructions only when the response supplies a vault destination and chain with an observed balance. While processing, it can show destination metadata, but that does not request an additional transfer. If these are missing, ask the operator for the missing information before recommending a transfer. A zero balance without a purchase amount has no quantified shortfall.

- `Funding: more funds needed`: if no transfer has been submitted, follow [Vault and funds](vault-and-funds.md#fund-the-vault) for verified deposit instructions. After a submitted transfer, follow the waiting rule below before recommending more funding.
- `Funding: blocked; operator action required`: report the block and ask the operator to investigate. Another deposit is not a remedy for frozen or reconciliation-blocked funds, or for setup that needs an operator.
- `Funding: processing`: follow the [processing limit](debug.md#processing-states). A pending observation is not a reason for a second deposit.
- `Funding: setup needed`: follow [Vault and card](vault-and-card.md).
- `Funding: ready for payment`: the service reports enough available funds. Readiness is an observation, not a reservation. Authorization checks funds again. Without a positive required amount, this result covers only the reported balance. Check against the verified USDC funding amount before checkout.

A purchase funding check is complete when the service reports readiness for its verified positive required amount. Otherwise, report the state and the missing action.

## After a submitted transfer

Check the same participant, vault, chain and required amount. A wallet receipt alone is insufficient: the observer must confirm the transfer and the service must record the funds before they count as available.

The command polls `processing` automatically, but returns immediately for `funds_needed`. If it still reports more funds needed after a submitted transfer, repeat the same funding command at most twice, waiting 5 seconds between checks. For `processing`, use the existing [processing limit](debug.md#processing-states).

Continue to checkout only when the service reports readiness for the required amount. If the checks end first, report that readiness is still unconfirmed, include the last returned state, and resume the same check when the user asks or new information arrives. Do not recommend another transfer solely because `funds_needed` persists: a submitted transfer may not yet be reflected in the recorded balance. Pending ledger evidence returns `processing`; frozen or reconciliation-blocked funds, and setup that needs an operator, return `error` and require operator help. Ask the operator to investigate a confirmed transfer that remains uncredited before recommending another deposit. Readiness confirms sufficient available funds, not receipt of a specific transaction.

## Errors

- `Required amount must be a whole number of minor units`: use the verified USDC funding amount as an integer from 0 through 18446744073709551615.
- `Funding response ...`: follow [Debug](debug.md); the response does not match the expected contract.
- `failed with HTTP 503. (service_error=ParticipantUnavailable ...)`: the provider is busy. Wait, then run the same command again under the [backoff budget](debug.md#retries). Another deposit does not help.
