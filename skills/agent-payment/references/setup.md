# Setup

Use Python 3.10 or later on Linux or macOS.

## Before you start

Confirm these with the user before the first purchase. The agent can create and fund the wallet, or guide the user. See [Owner wallet](wallet-setup.md).

- An owner wallet: an EVM wallet on Ink Sepolia. The service refuses an owner address that another participant already uses, so each participant needs its own address.
- Ink Sepolia: chain ID `763373`, RPC `https://rpc-gel-sepolia.inkonchain.com`.
- Test ETH for gas and test USDC on Ink Sepolia in that wallet.

The `funding` command prints the chain ID, token address and faucet links. Use its values if they differ from this list. Try its faucet links first. If they fail, follow [Faucets](faucets.md).

Run `python3 scripts/register.py check` from the skill directory after each configuration change. Interpret the result with [Check the setup](../SKILL.md#check-the-setup).

## Service URL

The scripts use the UAT gateway, `https://api.sandbox.services.payward.com`, unless `PWS_SERVICE_URL` is set. Set it only to reach another gateway or a local fixture server:

```bash
export PWS_SERVICE_URL="https://<gateway-host>"
```

The URL needs the `https` scheme and a host only. The scripts refuse a path, a query, a fragment, and userinfo. They accept cleartext `http` only for `localhost`, `127.0.0.1` and `::1`, and they send a loopback request direct, past every proxy.

`register --service-url <url>` and `check --service-url <url>` override the variable for one run.

## Credentials file

The scripts save the session to the first path that applies:

1. `$PWS_CREDENTIALS_FILE`
2. `$XDG_CONFIG_HOME/pws/agent-payment/credentials.json`
3. `~/.config/pws/agent-payment/credentials.json`

`PWS_CREDENTIALS_FILE` and `XDG_CONFIG_HOME` need an absolute path. Pick a path outside every Git checkout, because the scripts refuse a path under a checkout to keep the token out of a commit.

The scripts create each new directory with mode 0700 and the file with mode 0600. They refuse a saved file that the group or other users can read.

`--credentials <path>` overrides the variables for one run.

Keep each participant in its own credential file. To create a separate participant, use a new `--credentials` path and pass that path to every subsequent command (or set `PWS_CREDENTIALS_FILE`). Preserve the previous file: registration does not renew its session or recover its vault and card.


## Continue the saved setup

Run `python3 scripts/register.py setup` to resume the same saved participant.
The helper waits up to five minutes across its initial status read, initialize
requests, and polling. It prints each newly confirmed step and piece of setup
evidence once. Let it finish without prompting the user between processing
steps. It never registers a participant or switches the owner.

Only supported `PENDING` issuer steps resume initialization. Other pending
steps are observed through status reads. If setup waits for a deposit, the
helper prints the funding command immediately while it continues observing.
Funding still needs the user's authorized wallet transfer.

Ready, an explicit `NEEDS_OPERATOR`, or the deadline ends the wait. HTTP errors
and invalid responses also stop immediately; an uncertain write is not retried.
At the deadline, report the last state and printed recovery command, then end
the current setup attempt. Do not automatically start another five-minute run.
The printed `setup` command resumes the same saved setup on a later attempt.
After an operator stop, wait for the operator to resolve it before
resuming. Keep the same credentials path. Do not register again.

The deadline caps sleeps and each HTTP socket timeout to the remaining budget.
Python's HTTP timeout is per socket operation, so DNS or a slowly streamed
response can exceed that total. No further request starts after the deadline.

## Reconcile a submitted vault deployment

Ordinary `setup` stops at `NEEDS_OPERATOR`; it does not retry a deployment.
When the user explicitly asks to reconcile a stopped vault deployment, use the
same saved session:

```bash
python3 scripts/register.py setup --reconcile-vault
```

This command requires `vault_deployment` with a saved owner, vault, chain, and
32-byte `deploymentTxId`. It sends one initialize request with the recorded
owner and checks that the returned binding stays unchanged. An optional
`--owner-address` must match the saved owner. Other steps and incomplete
records are refused before a POST.

The backend supports this recovery only with shared runtime storage. It checks
the recorded transaction without broadcasting or replacing a transaction. A
confirmed matching deployment can proceed to issuer setup. An unresolved
transaction remains stopped for operator help. A deployment without a saved
hash still needs manual investigation.

After progress, the helper continues within the same five-minute deadline.
It does not repeat vault reconciliation. If status reports a supported pending
issuer step, it can resume that step with the same saved owner while checking
the recorded vault, chain, and transaction. Each server call bounds approval
work; the helper continues unfinished approval within its total deadline.
Reap KYC rejection and uncertain account, card, or enrollment writes still need
an operator. Preserve the credentials and transaction after an error; do not
register another participant or use another owner. Reconciliation does not
prove card, funding, or checkout readiness.
