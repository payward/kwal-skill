# Kwal Agent Payment

Guide an agent through Kwal sandbox registration, vault and card setup, test
funding, and purchases that you approve. Sandbox purchases do not ship goods.

Requires Python 3.10 or later on Linux or macOS. The helpers use only the Python
standard library. You need access to the Kwal sandbox gateway and your own wallet;
see [setup and prerequisites](skills/agent-payment/references/setup.md).

## Install

This repository supports skill installation and includes a complete plugin and
marketplace.

### Skills CLI (Claude Code and Codex)

With Node.js and npm installed, run from your project directory:

```sh
npx skills add payward/kwal-skill --skill agent-payment --agent claude-code codex
```

Start a new session and invoke the `agent-payment` skill explicitly.

### Claude Code plugin

Run in your terminal:

```sh
claude plugin marketplace add payward/kwal-skill
claude plugin install agent-payment@kwal-agent-payment
```

Start a new session and invoke `/agent-payment:agent-payment`.
For a one-session trial, use `claude --plugin-dir /absolute/path/to/kwal-skill`.

### Codex plugin

Run in your terminal:

```sh
codex plugin marketplace add payward/kwal-skill
```

In the desktop Plugins Directory, select **Kwal Agent Payment** and install
**Kwal Sandbox Payments**. Recent CLIs also support:

```sh
codex plugin add agent-payment@kwal-agent-payment
```

Start a new task and select **Kwal Sandbox Payments** with the skill picker
(`$` in the CLI). Codex lists it as `agent-payment:agent-payment`. Both clients
require you to invoke this skill explicitly.

### First run

From the installed skill directory, containing `SKILL.md`:

```sh
python3 scripts/register.py --help
python3 scripts/register.py check
```

`check` makes no service call. A missing session before registration is expected;
it is not a reason to reinstall. Invoke the skill when you are ready to register.
See [the workflow](skills/agent-payment/SKILL.md) and
[troubleshooting](skills/agent-payment/references/debug.md).

## Configuration and updates

The default gateway is `https://api.sandbox.services.payward.com`.
`PWS_SERVICE_URL` selects another gateway. Credentials are stored outside the
plugin, normally at `~/.config/pws/agent-payment/credentials.json`;
`PWS_CREDENTIALS_FILE` selects another absolute path.
See [configuration](skills/agent-payment/references/setup.md) for details.

Refresh the marketplace,
and update/reinstall the plugin in your client. Start a new session afterward.
To uninstall, remove the plugin in the client's plugin manager. Updating,
reinstalling, or removing the plugin does not renew or delete your saved session.
Preserve that session to keep access to the same participant, vault, and card.

These instructions target local Claude Code and Codex execution. Web-chat
installation and execution have not been validated for this package.

## Check and package

From this directory, using Python 3.10 or later:

```sh
python3 package.py check
python3 package.py build --output /tmp/agent-payment-release
```

`check` builds and unpacks a ZIP in a temporary directory outside the checkout,
checks package metadata and local Markdown links, runs the helper's `--help`,
and runs both packaging and skill fixture tests from the extracted package.
The tests use loopback HTTP fixtures, not the sandbox gateway. Rust and Git are
not required. `build` writes a versioned ZIP and its SHA-256 checksum.

Only the declared source files are packaged. Credentials, bytecode caches,
and archives are excluded. Keep generated releases outside
the source directory.

The root `plugin.json` is the release-version source. Keep the versions in
`.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` aligned; `check`
rejects a mismatch. Update [CHANGELOG.md](CHANGELOG.md) for each release.

## Service compatibility

Version 0.1.0 packages the existing Kwal sandbox participant v1 helpers. Its
fixture suite checks the API response shapes those helpers support. Installation
and fixture tests do not establish acceptance by a deployed service. Before
releasing against a changed backend, run the agreed sandbox journey and record
the tested service revision in the release notes.
