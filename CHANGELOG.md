# Changelog

## 0.2.0

- Add an owner wallet guide with two modes. In agent mode, the agent creates a
  test wallet, bridges Sepolia ETH to Ink Sepolia, and sends USDC to the vault.
  In guided mode, the user funds their own wallet.
- Add a faucet guide with dated observations, and a wallet recovery guide.
- Replace the failed Ink faucet instruction. Use Google Cloud Sepolia ETH with
  the bridge, and Circle USDC on Ink Sepolia.
- The agent wallet needs `uv`. The payment helpers still use only the Python
  standard library.

## 0.1.0

- Package the existing Agent Payment skill, Python helpers, references, and
  fixture tests as a standalone plugin for local Claude Code and Codex use.
- Add local marketplace catalogs, installation instructions, and a reproducible
  ZIP with a SHA-256 checksum.
- Validate and test the extracted package as a standalone package.

Payment behavior, explicit invocation, and credential storage are unchanged.
