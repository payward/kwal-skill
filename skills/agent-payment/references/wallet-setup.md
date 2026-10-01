# Owner wallet

The vault needs an owner wallet. The wallet needs test ETH for gas and test USDC on Ink Sepolia. Get the owner address before the first [setup](vault-and-card.md).

Offer help with the wallet. Ask the user once which mode to use:

- **Agent mode**: the agent creates the wallet, gets test funds, and sends USDC to the vault. The agent asks the user only for faucet logins, CAPTCHAs, and approvals. Recommend this mode when you can run shell commands, reach the network, and run `uv`.
- **Guided mode**: the user creates and funds their own wallet. The agent gives each step and checks the results.

Agent mode supports macOS and Linux. On Windows, use guided mode, or agent mode inside WSL.

## Network values

| Item | Ink Sepolia | Ethereum Sepolia |
|---|---|---|
| Chain ID | `763373` | `11155111` |
| RPC | `https://rpc-gel-sepolia.inkonchain.com` | `https://ethereum-sepolia-rpc.publicnode.com` |
| Circle USDC | `0xFabab97dCE620294D2B0b0e46C68964e326300Ac` | `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` |
| Explorer | `https://explorer-sepolia.inkonchain.com` | `https://sepolia.etherscan.io` |

The vault is on Ink Sepolia. Ethereum Sepolia is only a source of test ETH. The `funding` command prints the service values. If they differ from this table, use the service values and tell the operator.

## Guided mode

1. The user creates or selects an EVM wallet, for example MetaMask.
2. The user adds Ink Sepolia with the values above, and imports the Ink Sepolia Circle USDC contract.
3. The user gets test ETH on Ink Sepolia and test USDC on Ink Sepolia. Follow [Faucets](faucets.md).
4. The user gives only the public address. Use it as `--owner-address` for [setup](vault-and-card.md).
5. To fund the vault, the user sends USDC on Ink Sepolia to the vault address that the `funding` command prints. Follow [Fund the vault](vault-and-funds.md#fund-the-vault).

## Agent mode

### Before you start

1. Run `uv --version`. If `uv` is missing, ask the user to install it, or to let you install it. Use the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/), for example `brew install uv` on macOS. Install it only after the user agrees.
2. Tell the user these facts before you create the wallet:
   - The private key is a plain-text file on the host that runs the agent. Anyone who can read that file can use the wallet.
   - On a temporary or remote host, such as a container or a cloud sandbox, the key and its funds can be lost when the host stops. The user can import the key into their own wallet to keep it. See [Wallet recovery](wallet-recovery.md).
   - Use the wallet only for test assets.

Run each command from the skill directory:

```bash
uv run --locked --script scripts/wallet.py <command>
```

`--help` lists the commands, their options, and their limits.

### Procedure

1. Run `create`, then `info`. Continue only when `recovery_verified` is `true`. Give the user the address and the key file path. The key stays in its file.
2. Use the address as `--owner-address` for [setup](vault-and-card.md).
3. Run `balance --network ink`. One USDC transfer needs much less than 0.001 ETH for gas.
4. If Ink Sepolia has no ETH, get Sepolia ETH and bridge it. Follow [ETH for gas](faucets.md#eth-for-gas).
5. If the USDC is less than the amount to send, claim USDC. Follow [USDC](faucets.md#usdc).
6. Run the `funding` command from [Funding and readiness](funding.md) to get the vault address and the shortfall. Tell the user the vault address and the amount, then send:

   ```bash
   uv run --locked --script scripts/wallet.py transfer --to "<vault-address>" --usdc "<amount>" --request-id fund-1
   ```

   `--usdc` is in USDC, not minor units: the funding amount `1500000` is `--usdc 1.5`. The user's request to buy or to fund the vault authorizes the transfer.
7. After the transfer is confirmed, follow [After a submitted transfer](funding.md#after-a-submitted-transfer).

### Result states

Each command prints JSON. Read the `state` field.

- `confirmed`: the transfer is in a block. Follow [Funding and readiness](funding.md).
- `pending`: wait 30 seconds, then run the same command again. Allow up to ten runs.
- `bridge_pending`: the Sepolia deposit is confirmed. Wait 60 seconds, then run the same command again. Allow up to 15 minutes.
- `bridged`: the ETH is on Ink Sepolia. Run `balance --network ink`.
- `funds_needed`: the wallet needs more ETH or USDC on the reported network. Follow [Faucets](faucets.md), then run the same command again.
- `unresolved`: the script cannot find the broadcast result. Check the hash in the explorer and the RPC. Keep the transaction journal. Retry only with the same request ID.

If the state stays `pending` after ten runs, or `bridge_pending` after 15 minutes, give the user the hash and stop.

If a command fails, it prints an `error` message and exits with status 1. Correct the reported cause before you try again. A reverted transfer needs receipt inspection and a decision from the user.

### Request IDs

The request ID makes a transfer or a deposit safe to retry. Use the same ID until its result is known. Use a new ID only for a new intended transfer, for example `fund-2`. A run with the ID of a confirmed transfer sends nothing. A run with a known ID and different values fails.

### Files

The script keeps the wallet in `$XDG_CONFIG_HOME/pws/agent-payment/wallet/`, or `~/.config/pws/agent-payment/wallet/`. It refuses a location inside a Git checkout. Directories have mode 0700, and files have mode 0600.

- `private-key.txt`: the private key. It is a secret.
- `wallet.json`: the public address.
- `transactions-ink.json` and `transactions-ethereum.json`: the transaction journals. They contain signed transactions. Keep them private, and keep them through retries.

The script keeps one wallet. A second participant needs another owner address, so use guided mode for it.

If an RPC fails, set `TESTNET_INK_RPC` or `TESTNET_ETHEREUM_RPC` to another HTTPS RPC for the same chain.
