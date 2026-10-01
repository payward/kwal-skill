# Vault and card

Setup initializes or resumes the vault and sandbox card for the saved participant. Use the owner address supplied by the user, or the agent wallet address. If neither exists, follow [Owner wallet](wallet-setup.md).

```bash
python3 scripts/register.py setup --owner-address "<owner-address>"
python3 scripts/register.py setup
```

Replace `<owner-address>` with the address from the user or from the agent wallet `info` command. The command refuses the placeholder. The service refuses an owner address that another participant already uses, so a second participant needs a new wallet address. Show the address to the user before the first setup: checking its format does not prove control of its key. A deposit is not proof of owner control either.

Give `--owner-address` for the first run. Omit it to check or resume the same setup. To read the state without resuming a step, run `python3 scripts/register.py status`. The service keeps the first owner address. If the response includes `ownerAddress`, the helper displays it and rejects a supplied address that differs, without starting initialization. It also rejects a changed or missing owner during polling after the service has reported one. Older responses without owner evidence remain readable, but the helper marks the owner match unverified. Never treat an absent owner field as confirmation of control or permission to replace a vault.

For pending issuer steps (`issuer_setup`, `sandbox_approval`, `account_creation`, `account_verification`, or `card_verification`), the command keeps resuming initialization using the saved owner reported by the service, within one five-minute deadline. It can also resume `card_enrollment` when the card is active and no enrollment is recorded. Without that owner, it only reads status. Other pending steps are read-only. The command never intentionally replaces the vault or card.

## What the state proves

- A reported vault address means the service has a vault to report. It does not prove that a sandbox card is issued, enrolled, or ready for checkout.
- `Sandbox card: active (issuance, not enrollment)` means the service reports a validated issued card. `unverified` does not establish active issuance. Neither value proves enrollment.
- `deposit_observation` means setup is waiting for its deposit evidence. Use [Funding](funding.md) to read the deposit instructions and current balance. A deposit observation is not a spending guarantee.
- `Enrollment: <id> (active)` reports the saved enrollment separately from card issuance. `card_enrollment` remains unfinished until enrollment is active. The user supplies no card details. Ask the service operator about unresolved enrollment; do not invent an enrollment identifier or claim that it is active.
- A sandbox that pays with simulated card spends needs no enrollment: setup can report ready with an active card and an observed deposit, and no `Enrollment:` line.
- Only `Setup: ready for checkout` reports setup completion. Then run the funding command with the verified purchase funding amount in USDC minor units. Follow the [funding conversion limits](funding.md); do not guess a conversion from a quote in another currency. Setup readiness alone is not a balance check.

`Setup: processing` reports a confirmed pending step. Let the command keep going without asking the user to continue. It prints new evidence once and resumes the supported issuer steps above after checking status. At `deposit_observation`, it immediately prints the funding command so the user can arrange an authorized deposit. At the five-minute deadline, it prints the command to resume the same setup with the same credentials. See [Continue the saved setup](setup.md#continue-the-saved-setup).

`Setup needs an operator at <step>` means that the service cannot complete that step. The command stops immediately, including at `vault_deployment`. Report the step and ask the operator to complete the same participant setup. A later `python3 scripts/register.py status` is read-only; background vault recovery may have advanced the saved deployment. Do not retry an operator-stopped initialize automatically, register again, or create a replacement participant to recover it.

If the command fails or times out, follow [Debug](debug.md). Read status before retrying the same setup; a timeout does not prove that a provider write failed.

## Errors

- `Setup has not started`: ask the user for the vault owner address, or use the agent wallet address from [Owner wallet](wallet-setup.md). See the setup steps above.
- `Setup response ...`: the response does not match the participant API contract, or owner evidence conflicts with the request. Report the error and ask the service operator to verify the same setup.
