"""Autenticación simulada: no se conecta con Riot ni se aceptan partidas."""

import base64
import json
import threading
import unittest
from unittest.mock import Mock

import requests

from client_types import Credentials, LCUError
from lcu_client import HTTP_TIMEOUT, LCUClient
from riot_auth import (AUTHORIZATION_PATH, AUTHORIZATIONS_PATH, CLIENT_ID,
                       TFT_AUTHORIZATION_PATH, RiotClientAuth)


SUBJECT = "11111111-1111-4111-8111-111111111111"
OTHER_SUBJECT = "22222222-2222-4222-8222-222222222222"


def jwt(**changes):
    claims = {"sub": SUBJECT, "cid": CLIENT_ID, "exp": 2000}
    claims.update(changes)
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "RS256"}) + "." + encode(claims) + ".unit-test"


def authorized(token=None):
    return {"type": "authorized", "authorization": {
        "accessToken": {"token": jwt() if token is None else token}}}


def response(data=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class RiotAuthTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.event = threading.Event()
        self.credentials = Credentials(12345, "UNIT_LOCAL_SECRET")
        self.auth = RiotClientAuth(session=self.session, clock=lambda: 1000)
        self.auth._candidates = lambda cancelled: iter([self.credentials])

    def replies(self, *items):
        self.session.request.side_effect = [response({"subject": SUBJECT}), *items]

    def methods(self):
        return [call.args[0] for call in self.session.request.call_args_list]

    def assert_error(self, kind, callable_):
        with self.assertRaises(LCUError) as caught:
            callable_()
        self.assertEqual(caught.exception.kind, kind)
        return caught.exception

    def test_existing_authorization_is_read_without_post_or_wrong_client(self):
        self.replies(response(authorized()))
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.auth.subject, SUBJECT)
        self.assertEqual(self.methods(), ["GET", "GET"])
        self.assertEqual([c.args[1] for c in self.session.request.call_args_list], [
            "https://127.0.0.1:12345" + AUTHORIZATION_PATH,
            "https://127.0.0.1:12345" + TFT_AUTHORIZATION_PATH,
        ])
        self.assertEqual(self.session.auth, ("riot", "UNIT_LOCAL_SECRET"))
        self.assertFalse(self.session.trust_env)
        self.assertFalse(self.session.verify)
        for call in self.session.request.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": HTTP_TIMEOUT, "allow_redirects": False})

    def test_missing_authorization_is_created_once_then_verified_by_get(self):
        post = response(status=204)
        self.replies(response(status=404), post, response(authorized()))
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.methods(), ["GET", "GET", "POST", "GET"])
        request = self.session.request.call_args_list[2]
        self.assertEqual(request.args[1], "https://127.0.0.1:12345" + AUTHORIZATIONS_PATH)
        self.assertEqual(request.kwargs, {
            "json": {"clientId": CLIENT_ID, "keepAlive": False},
            "timeout": HTTP_TIMEOUT, "allow_redirects": False,
        })
        post.json.assert_not_called()
        post.close.assert_called_once()

    def test_not_authorized_uses_the_same_single_create_flow(self):
        self.replies(response({"type": "needs_authentication"}),
                     response(authorized()), response(authorized()))
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.methods(), ["GET", "GET", "POST", "GET"])

    def test_read_only_mode_never_creates_missing_or_unauthorized_authorization(self):
        for absent in (response(status=404), response({"type": "needs_authentication"})):
            with self.subTest(status=absent.status_code):
                self.setUp()
                self.auth = RiotClientAuth(session=self.session, clock=lambda: 1000,
                                           allow_authorization=False)
                self.auth._candidates = lambda cancelled: iter([self.credentials])
                self.replies(absent)
                self.assert_error("auth", lambda: self.auth.get_token(self.event))
                self.assertNotIn("POST", self.methods())

    def test_timeout_after_create_reads_authorization_before_returning_token(self):
        self.replies(response(status=404), requests.Timeout("UNIT_LOCAL_SECRET"),
                     response(authorized()))
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.methods(), ["GET", "GET", "POST", "GET"])

    def test_timeout_without_authorization_never_repeats_post_after_reactivation(self):
        self.replies(response(status=404), requests.Timeout("UNIT_LOCAL_SECRET"),
                     response(status=404), response({"subject": SUBJECT}), response(status=404))
        self.assert_error("timeout", lambda: self.auth.get_token(self.event))
        self.auth.disconnect()
        self.assert_error("auth", lambda: self.auth.get_token(threading.Event()))
        self.assertEqual(self.methods().count("POST"), 1)
        self.assertNotIn("UNIT_LOCAL_SECRET", repr(self.credentials))

    def test_new_credentials_allow_a_new_authorization_attempt(self):
        self.replies(response(status=404), response(status=204), response(status=404),
                     response({"subject": SUBJECT}), response(status=404),
                     response(status=204), response(authorized()))
        self.assert_error("auth", lambda: self.auth.get_token(self.event))
        self.auth.disconnect()
        self.credentials = Credentials(54321, "ANOTHER_UNIT_SECRET")
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.methods().count("POST"), 2)

    def test_every_token_read_fetches_current_token_instead_of_caching(self):
        fresh = jwt(exp=3000)
        self.replies(response(authorized()), response(authorized(fresh)))
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.auth.get_token(self.event), fresh)
        self.assertEqual(self.methods(), ["GET", "GET", "GET"])

    def test_wrong_client_account_or_expired_token_never_creates_authorization(self):
        for bad in (jwt(cid="riot-client"), jwt(sub=OTHER_SUBJECT), jwt(exp=1000),
                    jwt(exp=True), jwt(exp=float("nan"))):
            with self.subTest(case=bad[-10:]):
                self.setUp()
                self.replies(response(authorized(bad)))
                self.assert_error("auth", lambda: self.auth.get_token(self.event))
                self.assertNotIn("POST", self.methods())

    def test_invalid_tokens_are_sanitized_and_never_sent_anywhere(self):
        for bad in ("UNIT_SECRET", "header.payload.signature\r\nAuthorization: UNIT_SECRET", "", 42):
            with self.subTest(type=type(bad).__name__):
                self.setUp()
                self.replies(response(authorized(bad)))
                error = self.assert_error("data", lambda: self.auth.get_token(self.event))
                self.assertNotIn("UNIT_SECRET", str(error))
                self.assertNotIn("POST", self.methods())

    def test_invalid_primary_subject_rejects_candidate_before_token_lookup(self):
        self.session.request.return_value = response({"subject": "not-a-uuid"})
        self.assert_error("data", lambda: self.auth.get_token(self.event))
        self.assertEqual(self.methods(), ["GET"])
        self.assertFalse(self.auth.connected)
        self.assertIsNone(self.auth.subject)

    def test_cancellation_before_discovery_makes_no_request(self):
        self.event.set()
        self.assert_error("cancelled", lambda: self.auth.get_token(self.event))
        self.session.request.assert_not_called()

    def test_cancellation_during_token_read_prevents_authorization_post(self):
        def request(method, url, **kwargs):
            if url.endswith(AUTHORIZATION_PATH):
                return response({"subject": SUBJECT})
            self.event.set()
            return response(status=404)
        self.session.request.side_effect = request
        self.assert_error("cancelled", lambda: self.auth.get_token(self.event))
        self.assertNotIn("POST", self.methods())

    def test_post_connection_error_does_not_reissue_post_on_next_call(self):
        self.replies(response(status=404), requests.ConnectionError("UNIT_SECRET"),
                     response(authorized()))
        self.assert_error("connection", lambda: self.auth.get_token(self.event))
        self.assertEqual(self.auth.get_token(self.event), jwt())
        self.assertEqual(self.methods().count("POST"), 1)

    def test_discovery_uses_credential_holder_process_and_no_lcu_lockfiles(self):
        checker = Mock(return_value=True)
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(
            '"Riot Client.exe" --app-port=12345 --remoting-auth-token=UNIT_LOCAL_SECRET')))
        auth = RiotClientAuth(session=Mock(), process_checker=checker, process_runner=runner)
        self.assertEqual(auth.lockfiles, ())
        self.assertEqual(list(auth._process_credentials(self.event)), [self.credentials])
        checker.assert_called_once_with("Riot Client.exe")
        self.assertIn("Name='Riot Client.exe'", runner.call_args.args[0][-1])
        self.assertNotIn("LeagueClientUx", runner.call_args.args[0][-1])

    def test_missing_process_has_no_network_or_powershell_work(self):
        checker, runner = Mock(return_value=False), Mock()
        auth = RiotClientAuth(session=self.session, process_checker=checker, process_runner=runner)
        self.assert_error("connection", lambda: auth.get_token(self.event))
        self.session.request.assert_not_called()
        runner.assert_not_called()

    def test_legacy_request_does_not_add_json_body(self):
        client = LCUClient(session=self.session, lockfiles=[])
        client.credentials = self.credentials
        self.session.request.return_value = response("Lobby")
        self.assertEqual(client.get_phase(self.event), "Lobby")
        self.assertNotIn("json", self.session.request.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
