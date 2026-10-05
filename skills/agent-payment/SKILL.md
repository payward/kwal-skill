---
name: agent-payment
description: Make sandbox purchases with Kwal while you keep control of your wallet and approve each payment.
disable-model-invocation: true
---

# Agent Payment

Run commands from the directory that contains this `SKILL.md`.

Before a first purchase, confirm the [prerequisites](references/setup.md#before-you-start) with the user.

Select the operation that matches the request:

- Inspect saved credentials: [show the session](#show-the-session).
- Check or configure local settings: [check the setup](#check-the-setup).
- Read the vault and card state without changing it: [read the status](#read-the-status).
- Create a participant: [register a participant](#register-a-participant).
- Create or check the vault and the sandbox card: [Vault and card](references/vault-and-card.md).
- Show test-funding instructions, or check whether a payment can be attempted: [Funding and readiness](references/funding.md).
- Find an item and resolve the variant to buy: [Product selection](references/products.md).
- Price an item and check the readiness for its total: [Quotes](references/quotes.md).
- Buy a priced quote, or read the result of a submitted checkout: [Checkout and payment status](references/checkout.md).
- Explain custody or balances, or help with an owner withdrawal: [Vault and funds](references/vault-and-funds.md).
- Send an authenticated request: [call a protected route](#call-a-protected-route).

## Purchase sequence

Resume an existing payment with [Checkout and payment status](references/checkout.md). For a new purchase:

1. Use a valid saved session, or [register a participant](#register-a-participant) when authorized.
2. Create or check the [vault and card](references/vault-and-card.md) for that session. Let `setup` continue through resumable processing for up to five minutes without asking the user to continue. Follow the command's next step on readiness, an operator stop, or the deadline. While setup is at `vault_deployment`, tell the user three things: vault provisioning is queued, it can take some minutes when many participants set up together, and the same setup continues. This wait is not an error.
3. [Fund the vault](references/funding.md) with test USDC from the owner wallet.
4. [Select a product](references/products.md) and resolve a purchasable variant.
5. [Prepare a quote](references/quotes.md), including required shipping choices and funding readiness for the final total. If the desired shipping option is already selected, keep it; see [Shipping selection](references/quotes.md#shipping-selection).
6. [Review, submit, and observe checkout](references/checkout.md) under the user's purchase authorization. The user approves the payment on the hosted page.

In the sandbox, the payment is a simulated card spend against your vault: Kwal approves it from your vault funds, and no shop ships anything. See [Sandbox payments](references/checkout.md#sandbox-payments-are-simulated-card-spends).

For custody, balances, funding details, or withdrawal, follow [Vault and funds](references/vault-and-funds.md).

## Check the setup

```bash
python3 scripts/register.py check
```

Exit 0 means the local configuration is usable and the saved credentials are unexpired. The command makes no service call; service acceptance remains unverified. To read the service state, [read the status](#read-the-status).

If configuration is missing or must change, follow [references/setup.md](references/setup.md). An absent session is expected before the first registration. Use the registration branch when the user requests it.

## Register a participant

Registration creates a new Reap sandbox participant and a seven-day JWT. It does not create the vault or card. There is no renewal or refresh: another registration creates a separate identity and cannot recover the previous participant's vault or card.

Use the user's request to create a participant as authorization:

```bash
python3 scripts/register.py register
```

`register` keeps an existing unexpired session and returns exit 0. Registration is complete when the command reports that it registered and saved the credentials. Save the token before starting setup; never print it or include it in logs.

For an intentionally separate participant, preserve the existing credentials and select a new absolute path outside every Git checkout:

```bash
python3 scripts/register.py register --credentials /absolute/private/path/new-participant.json
```

Pass that same `--credentials` path to subsequent commands or set `PWS_CREDENTIALS_FILE` to it. `--force` is refused. A request to renew does not authorize creating a different participant. For expired or unusable credentials, preserve the file and contact the operator.

Registration is not idempotent. After a timeout, lost or invalid response, or token-save failure, stop and ask the operator to reconcile the outcome before another registration.

## Show the session

```bash
python3 scripts/register.py show
```

The command prints the saved service URL and expiry. It prints no token.

## Read the status

```bash
python3 scripts/register.py status
```

The command reads the vault and card setup once. It never starts or resumes a step, so it is safe at any time. It prints the setup state and the next step. `Setup: stopped for operator help at <step>` means the participant needs the operator.

## Call a protected route

Prefer the commands above. Select the route, HTTP method, and JSON fields from the target service API documentation. Use an absolute path for a request body stored outside the skill directory.

```bash
python3 scripts/register.py call /kwal/participant/v1/status
python3 scripts/register.py call /kwal/participant/v1/payments --method POST --body-file /absolute/path/to/body.json
```

A route that has no GET, such as `/kwal/participant/v1/payments`, answers a GET with HTTP 405.

`call` uses the service URL stored with the credentials. `check` and a new registration use `PWS_SERVICE_URL` when it is set, and the UAT gateway otherwise.

The command prints the JSON response. For Python calls, add this skill's `scripts/` directory to `sys.path`, then import `pws_client`.

## Failures

If a command fails, follow [references/debug.md](references/debug.md).
