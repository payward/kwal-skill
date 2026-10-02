"""Copying a suggested command must keep the selected participant."""

import io
import os
import shlex
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from support import CommandTests, StubService, TOKEN

import pws_client
import register

OWNER = "0x" + "cd" * 20
READY = {
    "state": "PARTICIPANT_SETUP_STATE_READY",
    "ownerAddress": OWNER,
    "vaultAddress": "0x" + "ab" * 20,
    "chain": "eip155:763373",
    "cardStatus": "ACTIVE",
    "depositObserved": True,
}
NOT_STARTED = {"state": "PARTICIPANT_SETUP_STATE_NOT_STARTED"}


class IdentityGuidanceTests(CommandTests):
    def setUp(self):
        super().setUp()
        self.path = self.path.with_name("team a's credentials.json")

    def expected_command(self, command):
        return f"python3 scripts/register.py {command} --credentials {shlex.quote(str(self.path))}"

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = register.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_copied_first_setup_uses_selected_identity_despite_another_default(self):
        with StubService(responses=(NOT_STARTED, NOT_STARTED, READY)) as selected:
            with StubService(payload=READY) as unrelated:
                default = self.path.with_name("default.json")
                pws_client.CredentialStore(default).save(pws_client.Credentials(
                    unrelated.url, "another.participant.token", int(time.time()) + 3600,
                ))
                with patch.dict(os.environ, {"PWS_CREDENTIALS_FILE": str(default)}):
                    code, out, err = self.run_command("status", selected.url)
                    self.assertEqual((code, err), (0, ""))
                    command = out.split("then run: ", 1)[1].strip()
                    argv = shlex.split(command.replace("<address>", OWNER))[2:]
                    code, _, err = self.main(argv)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(unrelated.calls, [])
        self.assertEqual(selected.calls, [
            ("GET", pws_client.STATUS_PATH),
            ("GET", pws_client.STATUS_PATH),
            ("POST", pws_client.INITIALIZE_PATH),
        ])
        self.assertEqual(selected.authorizations, [f"Bearer {TOKEN}"] * 3)

    def test_status_next_steps_keep_selected_credentials(self):
        cases = (
            (READY, "funding"),
            ({**READY, "state": "PARTICIPANT_SETUP_STATE_PENDING", "step": "deposit_observation"}, "funding"),
            ({"state": "PARTICIPANT_SETUP_STATE_PENDING", "step": "sandbox_approval", "ownerAddress": OWNER}, "setup"),
        )
        for body, next_command in cases:
            with self.subTest(next_command=next_command, state=body["state"]):
                with StubService(payload=body) as service:
                    code, out, err = self.run_command("status", service.url)
                self.assertEqual((code, err), (0, ""))
                for line in out.splitlines():
                    if "python3 scripts/register.py" in line:
                        self.assertIn("--credentials", line)
                self.assertIn(self.expected_command(next_command), out)
                self.assertEqual(service.calls, [("GET", pws_client.STATUS_PATH)])

    def test_local_check_next_step_keeps_selected_credentials(self):
        with StubService(payload={}) as service:
            code, out, err = self.run_command("check", service.url)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(self.expected_command("status"), out)
        self.assertEqual(service.calls, [])

    def test_service_recovery_keeps_selected_credentials(self):
        unauthenticated = {"type": "tag:kraken.com,2025:ParticipantUnauthenticated"}
        with StubService(status=401, payload=unauthenticated) as service:
            code, _, err = self.run_command("funding", service.url)
        self.assertEqual(code, 1)
        self.assertIn(self.expected_command("check"), err)
        self.assertNotIn(TOKEN, err)

    def test_busy_setup_recovery_keeps_selected_credentials(self):
        unavailable = {"type": "tag:kraken.com,2025:ParticipantUnavailable"}
        with StubService(responses=(NOT_STARTED, (503, unavailable))) as service:
            code, _, err = self.run_command("setup", service.url, "--owner-address", OWNER)
        self.assertEqual(code, 1)
        self.assertIn(self.expected_command("status"), err)
        self.assertNotIn(TOKEN, err)

    def test_missing_credentials_recovery_retains_requested_destination(self):
        code, _, err = self.main(["check", "--credentials", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn(self.expected_command("register"), err)

    def test_invalid_owner_recovery_retains_selected_participant(self):
        with StubService(payload=NOT_STARTED) as service:
            code, _, err = self.run_command("setup", service.url, "--owner-address", "invalid")
        self.assertEqual(code, 1)
        self.assertIn(self.expected_command("setup --owner-address <address>"), err)
        self.assertEqual(service.calls, [])
