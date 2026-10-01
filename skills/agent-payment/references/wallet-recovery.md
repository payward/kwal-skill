# Wallet recovery

Use this guide to move the agent wallet into a wallet app of the user, or to back it up. Do it before a temporary or remote host stops.

The wallet directory is `$XDG_CONFIG_HOME/pws/agent-payment/wallet/`, or `~/.config/pws/agent-payment/wallet/`. See [Files](wallet-setup.md#files).

## Import into MetaMask

The user does these steps on the host that has the key. The user copies the key; the agent gives only the file path.

1. In MetaMask, open the account menu and select the option to import an account with a private key.
2. Open `private-key.txt` locally. Copy its contents into MetaMask.
3. Compare the imported address with the address in `wallet.json`.
4. Add Ink Sepolia with the values from `uv run --locked --script scripts/wallet.py networks`. Enable test networks if MetaMask hides them.
5. Import Circle USDC with the Ink Sepolia contract from `networks`.

This is a private-key import. It does not give a recovery phrase. The import does not move funds. The vault owner stays the same address.

## Back up and restore

1. Copy the complete wallet directory to private storage. Include the transaction journals.
2. To restore, put the directory back at the same path. Keep mode 700 on directories and mode 600 on files.
3. Run `uv run --locked --script scripts/wallet.py info`. Continue only when `recovery_verified` is `true`.
