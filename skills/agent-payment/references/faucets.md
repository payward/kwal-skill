# Faucets

Use this guide when the faucet links from the `funding` command fail. Faucets change often. The results below were observed on 2026-10-01. They are not permanent.

The owner wallet needs two assets on Ink Sepolia:

- ETH for gas. One USDC transfer needs much less than 0.001 ETH.
- Circle USDC for the vault.

Submit only the public owner address. Read the balance before and after each claim. A success page alone does not prove receipt.

## How to claim

Faucets need a login, a CAPTCHA, or other checks. The user does these steps.

1. Send the user the faucet link, the network, the asset, and the owner address.
2. Offer to open the faucet in the built-in browser of the agent. That browser often does not have the logins of the user. Open it only when the user confirms. The user does the login and the CAPTCHA.
3. Wait for the user to confirm the claim, then read the balance.

Do not bypass a CAPTCHA, a cooldown, or an eligibility check. Do not use private faucet endpoints. Do not create extra identities or addresses to get more funds.

## USDC

Use [Circle's faucet](https://faucet.circle.com):

1. Select USDC and **Ink Sepolia**.
2. Enter the owner address and select **Send 20 USDC**.
3. Wait for **Tokens sent**, then read the balance.

Circle gave 20 USDC for each address and network every 2 hours. Read the page for the current limit. The page can show a reCAPTCHA.

## ETH for gas

### Recommended path

1. Use [Google Cloud's Sepolia faucet](https://cloud.google.com/application/web3/faucet/ethereum/sepolia). Select Ethereum Sepolia, enter the owner address, and request ETH. It needs a Google login. A new wallet got 0.05 ETH. Google can apply eligibility checks.
2. Move the ETH from Ethereum Sepolia to the same address on Ink Sepolia:
   - Agent mode: run the bridge command. It needs Sepolia gas but no Ink gas.

     ```bash
     uv run --locked --script scripts/wallet.py bridge --request-id ink-gas-1 --eth 0.02
     ```

     Follow the [result states](wallet-setup.md#result-states). The deposit took a few minutes. Keep the same request ID until the state is `bridged`. A new ID for a pending deposit can send the amount two times. One pending Sepolia transaction blocks the next deposit.
   - Guided mode: the user uses the [Ink bridge](https://inkonchain.com/bridge). If it fails, the user uses [Superbridge testnets](https://testnets.superbridge.app).
3. Read the Ink Sepolia balance.

### Other options

Offer these options if the user declines the Google faucet, or if it fails. They were not verified end to end.

| Option | Network | Observation on 2026-10-01 |
|---|---|---|
| [Ink faucet](https://inkonchain.com/faucet) | Ink Sepolia | Failed |
| [Gelato faucet](https://faucet-gel-sepolia.inkonchain.com) | Ink Sepolia | HTTP 525 and 404 |
| [Superchain faucet](https://console.optimism.io/faucet) | Ink Sepolia | Needs a login |
| [QuickNode](https://faucet.quicknode.com/ink/sepolia) | Ink Sepolia | Rejected a new wallet |
| [pk910 PoW faucet](https://sepolia-faucet.pk910.de) | Ethereum Sepolia | Not tested. Mining runs in the browser |
| [Alchemy](https://www.alchemy.com/faucets/ethereum-sepolia), Chainlink, Chainstack | Ethereum Sepolia | Need mainnet funds |

A person who has test ETH can send it to the owner address. [Ink's faucet list](https://docs.inkonchain.com/tools/faucets) shows more providers.

ETH on Ethereum Sepolia needs the bridge step. ETH on Ink Sepolia is ready to use.

## Sources

- [Circle USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses)
- [Ink network information](https://docs.inkonchain.com/general/network-information)
- [Ink contracts](https://docs.inkonchain.com/useful-information/contracts)
- [Deposit transaction format](https://specs.optimism.io/protocol/deposits.html)
