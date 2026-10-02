# Quotes

A quote prices a variant, quantity, shipping, and tax. It has an expiry time.

In the sandbox's local quote mode, quotes use fixed demo pricing of 1 USDC per
item, regardless of the catalogue price. Their IDs start with `sandbox_quote_`
and the helper labels the demo price. Nothing ships, so these quotes have no
shipping options or fees and expire after 15 minutes. Use the same quote and
checkout commands below. Funding checks and simulated card spending use the
saved demo total; card authorization and vault settlement still run normally.

```bash
python3 scripts/register.py quote --variant "<variant-id>" --email "<buyer-email>"
python3 scripts/register.py quote --variant "<variant-id>" --email "<buyer-email>" --quantity 2
python3 scripts/register.py quote-check "<quote-id>"
python3 scripts/register.py shipping "<quote-id>" --option "<shipping-option-id>"
```

Replace placeholders before execution. Use the buyer email and requested quantity; the default is one item. Give a delivery address when the user supplied one for this purchase or the service requires it. Ask only for missing required details. An address needs `--first-name`, `--last-name`, `--phone`, `--line1`, `--city`, and `--country` together. Use E.164 format for the phone, for example `+447700900123`. `--line2`, `--region`, and `--postal-code` are optional.

```bash
python3 scripts/register.py quote --variant "<variant-id>" --email "<buyer-email>" \
  --first-name "<first-name>" --last-name "<last-name>" --phone "<phone>" \
  --line1 "<street>" --city "<city>" \
  --postal-code "<postal code>" --country "<country>"
```

Read the returned quote id, items, quantities, total, currency, and expiry. Report subtotal, shipping, and tax when present.

For this UAT MVP, the backend converts USD prices to USDC at 1:1. The helper sends the quote ID for each funding check. The backend checks ownership and expiry, then uses the current provider total. For example, 19.02 USD requires 19,020,000 USDC base units. The helper shows the merchant total, required funding, token contract, and chain ID. Other currencies and token combinations are not supported. If the total changes, obtain authorization for the new total before checkout.

- `Shipping: selection required`: use the user's existing choice if it matches a returned option. Otherwise ask for a choice, then run `shipping`. Read the updated total.
- `Quote: expired`: refresh only a quote that has no submitted checkout. Preserve the variant, quantity, buyer email, delivery address, and shipping preference. Resolve the preference against the new shipping options; option ids can change. Review the new total and obtain authorization for the replacement before checkout.
- `Attached payment: <payment-id>`: a checkout already reserved this quote. Read that payment with `payment "<payment-id>"` and follow [Checkout](checkout.md). Do not submit another checkout and do not reprice the quote.
- `Funding: ...`: follow [Funding and readiness](funding.md). Use `quote-check` to recheck after a deposit or to resume an observation, because it uses the quote total. Apply the [processing limit](debug.md#processing-states).

Preparation is complete when the quote is unexpired, all required shipping choices are set, and funding is ready for its total. Give the quote id and total to the checkout step. Readiness does not reserve funds.

Funding uses six-decimal USDC. A USD cent is not a USDC minor unit. The helper
does not infer exchange rates. For a quote in other units, report the total, do
not continue to checkout, and tell the user that this MVP prices only USD.

## Shipping selection

The helper prints `Selected shipping option: <id>`. Keep it when it matches
the user's choice. Call `shipping` only to choose an option when none is
selected or to change the choice, then review the updated total. The helper
reads the quote first and skips the selection POST when the requested option
is already selected.

In Reap sandbox testing on 2026-09-28, posting the current option again returned
`SHIPPING_OPTION_INVALID`, while changing to another option and back worked.
This was observed for one merchant and basket; it is not a restriction on
changing shipping. Direct API callers should inspect `selectedShippingOptionId`
before posting a selection.

After a failed or uncertain selection, run `quote-check "<quote-id>"` before
another shipping write. If the unexpired quote already has the desired option,
use its current total. Follow the expiry or attached-payment guidance above
when applicable. Do not retry a rejected selection unchanged or switch options
solely to force a redundant selection through.

## Errors

- `Shipping address needs <flags>.`: supply the missing required fields, or omit the address if the purchase does not need one.
- `service_error=ParticipantFundingRequest`: the service cannot price this quote for funding. For an expired quote, create a new one. Otherwise the quote needs USD and the vault needs Ink Sepolia.
- `shipping` fails with `service_error=ParticipantBadRequest`: read the quote and follow [Shipping selection](#shipping-selection). This includes the provider's `SHIPPING_OPTION_INVALID` rejection. Older deployments returned `ParticipantUnavailable` for that rejection, so a shipping 503 alone does not prove an outage.
- A `/quotes` route fails with `service_error=ParticipantUnavailable`: the provider did not answer in time. Wait, then run the same command again under the [retry limit](debug.md#retries). The next quote create sends a new provider key.
- `quote-check` fails on the `/funding` route with `service_error=ParticipantUnavailable`: follow [Funding and readiness](funding.md#errors).
- An invalid quote response: follow [Debug](debug.md).
