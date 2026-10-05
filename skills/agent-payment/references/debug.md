# Debug

## Select the recovery by exit status

| Status | Meaning | Next step |
| --- | --- | --- |
| 0 | The command succeeded. | Use the completion criterion for the selected operation in [SKILL.md](../SKILL.md). |
| 1 | The command failed. | Follow the error-reporting procedure below. |
| 2 | The command line is wrong. | Read the usage output, correct the input, and run the command again. |

## Report a problem to the user

For exit 1, the command reports each problem with two lines on stderr:

```
error: <what failed>
action: <the next step>
```

Give both lines to the user verbatim. Apply the authorization rule in [Register a participant](../SKILL.md#register-a-participant) before you perform the action.

## Retries

Many participants share one provider quota. When the provider is busy, the service puts the request in a queue. The service can answer "unavailable" for several minutes. The queue moves without operator help. Keep the same command and wait.

Retry only when the operation is safe to repeat. Use the backoff budget for the error class:

| Error class | Error line | Backoff budget |
| --- | --- | --- |
| Provider busy | `service_error=ParticipantUnavailable` or HTTP 429 | Wait 15 to 30 seconds between attempts, for up to 5 minutes from the first failure. |
| No answer | HTTP 502, 503 or 504 without `service_error`, or `did not complete` | Wait 15 to 30 seconds between attempts, for up to 5 minutes from the first failure. |
| Unknown checkout outcome | `checkout` with HTTP 502, 503 or 504, or `did not complete` | Read the payment first. Then use the no-answer budget. See [Checkout errors](checkout.md#errors). |
| Registration | Any failure of `register` | No retry. See the next paragraph. |
| Input, credentials, or a permanent failure | Any other `service_error` | No retry. Follow the `action:` line. |

When the error line shows `retry_after=<n>s` and `<n>` is more than 15, wait `<n>` seconds before the next attempt. After 5 minutes, stop and report the error line and the last known state. If `setup` fails for 5 minutes, read the state with `python3 scripts/register.py status`. An operator stop needs the operator.

Never retry registration automatically. A timeout, lost or invalid response, or token-save failure may follow creation of a participant. Preserve any saved files and ask the operator to reconcile the provider outcome before another registration.

A read can be repeated. Repeat a write only when the service contract or the operation guide guarantees that the same request resumes the same operation. Preserve its identifiers. An unknown write outcome needs a status check or operator help before another write. For `checkout`, follow [Checkout errors](checkout.md#errors).

For a failed shipping selection, read the quote before another selection POST.
An already-selected option needs no new write; follow [Shipping selection](quotes.md#shipping-selection).

Correct invalid input or configuration before another attempt. Stop on a permanent failure, an invalid service response, or an exhausted backoff budget. Report the error and the last known state.

## Diagnose local configuration or credentials

Run from the directory that contains `SKILL.md`:

```bash
python3 scripts/register.py check
```

Use `check` when an error concerns local configuration or saved credentials. It reports all detected local problems in one run. After each fix, run it again and use the completion criterion in [Check the setup](../SKILL.md#check-the-setup).

## Missing information and service errors

- A required configuration value is unavailable: ask the user for that value. See [setup.md](setup.md).
- A saved session is unusable or expired: preserve its file, repair access or restore the same credentials where possible, and contact the operator. There is no renewal or refresh. A new registration cannot recover the existing vault or card.
- `Identifier must be ASCII text without spaces.`: use the id exactly as printed. If the printed id is invalid, report the API mismatch to the operator.
- The parser rejects a service response: report the error and ask the service operator to verify the API contract.

## Processing states

Let the command finish its built-in polling. `setup` automatically resumes supported pending steps for up to five minutes without prompting the user between steps. It prints new evidence once, prints the funding command when a deposit is needed, and stops on readiness, an operator stop, the deadline, or an error. At the deadline, report the last state and recovery command, then end this attempt. Do not automatically start another wait or repeat an operator-stopped or failed write. See [Continue the saved setup](setup.md#continue-the-saved-setup).

For other commands, if polling still reports a processing state, report that state and the identifier to the user, then end the current check. Run another check after relevant new information arrives or the user asks to continue. Each new check observes the same operation. A processing state does not authorize a replacement.
