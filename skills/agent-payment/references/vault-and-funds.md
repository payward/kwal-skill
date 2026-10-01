# Vault and funds

Use this reference to explain custody, a funding shortfall, or an operator-coordinated owner withdrawal. Confirm the deployed contract and its settings with the operator before giving transaction instructions.

## Custody

In guided mode, the user controls the owner wallet and keeps its private key. The user signs funding transactions and withdrawal requests in their wallet. In agent mode, the agent keeps a test wallet key on the host and signs the funding transfer. The user can import that key into their own wallet; see [Wallet recovery](wallet-recovery.md). The skill also stores a service session token. That token does not authorize an owner withdrawal. Card details and provider challenges stay on the hosted payment page.

## Fund the vault

Use the vault address and chain from the service response. A token symbol alone does not identify a token contract. Use the returned chain ID, token address, decimals and faucet links shown by the funding command. If the response lacks the vault destination, token address, chain, or transfer instructions, ask the service operator for the missing details before giving transaction instructions.

Give the user the reported shortfall and the verified funding instructions. The user needs the required token and gas on that chain. The current Kwal vault is funded by calling `transfer(vault_address, amount)` on the configured ERC-20 token contract. It has no deposit method and does not pull token allowances, so no token approval is needed. To get test funds, follow [Faucets](faucets.md). In agent mode, send the transfer with the wallet command; see [Owner wallet](wallet-setup.md#procedure).

After the deposit, follow [funding.md](funding.md) to observe the same vault. A deposit receipt alone does not prove payment readiness or owner control.

## Card-spendable and withdrawable funds

The token balance is the USDC held by the vault. Kwal holds deposited USDC for card spending. The contract's `available()` is the token balance minus the on-chain hold. When the hold equals the balance, `available()` is zero even if the card can spend the funds.

`Card-spendable` is the participant funding API's `available` amount: the service's observed capacity for card payments. It is not the contract's `available()` or an owner withdrawal quote. The API does not report an authoritative withdrawable amount, so the helper prints `Withdrawable: unknown (operator required)`. Do not substitute the card-spendable amount or assume zero.

Payment readiness also depends on the service accounting and processing state. A balance, deposit, or linked collection identifier does not prove that settlement has completed. Readiness does not reserve funds; payment authorization checks them again.

## Owner withdrawal

For the hackathon, withdrawals require the service operator. The participant API and this skill have no withdrawal command. Withdrawal is a separate user request; purchase completion does not request it.

Use this handoff:

1. Give the operator the saved participant identity, vault, chain, token, owner address, requested amount, and recipient. Keep the same participant and check for an existing withdrawal. Never share the session token or the owner's private key.
2. The operator checks outstanding card obligations and reduces card-spendable capacity before lowering the on-chain hold. External mode reserves the withdrawal in Kwal's ledger. Managed mode lowers the Reap mirror first. Lowering the hold alone bypasses this accounting.
3. Ask the operator to confirm the supported execution procedure for that deployment. The contract's co-signed path, `withdrawCosigned`, applies the approved hold and payout together; the owner submits it from their wallet with the operator's signature. The participant API cannot obtain that signature. Keep wallet signing with the owner and verify the amount and recipient before submission.
4. Report completion only after confirming the receipt, actual USDC transferred, and agreed recipient. The operator reconciles the remaining hold and card-spendable capacity. If submission or confirmation is uncertain, preserve the request and transaction details for reconciliation before another attempt.

Do not recommend `requestWithdraw` followed by `executeWithdraw` as a way to release card-held funds. Waiting does not lower the hold. Execution grants at most `available()` and clears the request even when it grants zero.

Delayed force release is an emergency exit after operator silence, not a routine withdrawal workaround. It releases the hold without transferring funds, and operator activity can cancel it. Escalate a withdrawal request to the operator instead of directing the user through that path.
