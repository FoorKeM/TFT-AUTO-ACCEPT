"""Backend TFT completamente simulado; ninguna petición ni aceptación reales."""

import threading
import unittest
from unittest.mock import Mock

import requests

from client_types import LCUError
from tft_backend import TFTBackendClient, HTTP_TIMEOUT, MAX_MEMBERSHIPS


SUBJECT = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
PARTY = "00000000-0000-4000-8000-000000000003"
HOST = "las-red.lol.sgp.pvp.net"


def player():
    return {"puuid": SUBJECT, "currentParty": {
        "partyId": PARTY, "product": "tft", "players": [{"puuid": SUBJECT, "partyProduct": "tft"}],
    }}


def rso_player():
    return {"puuid": SUBJECT, "currentParty": {
        "partyId": OTHER, "product": "lol", "players": [{"puuid": SUBJECT}],
    }, "parties": [
        {"puuid": SUBJECT, "partyId": OTHER, "partyProduct": "lol"},
        {"puuid": SUBJECT, "partyId": PARTY, "partyProduct": "tft"},
    ]}


def response(status=200, data=None):
    result = Mock(status_code=status)
    result.json.return_value = player() if data is None else data
    return result


class TFTBackendTests(unittest.TestCase):
    def setUp(self):
        self.cancelled = threading.Event()
        self.session = Mock()
        self.session.request.return_value = response()
        self.factory = Mock(return_value=self.session)
        self.provider = Mock(return_value="opaque-own-gaps-token")
        self.client = TFTBackendClient(HOST, SUBJECT, self.provider, session_factory=self.factory)
        self.addCleanup(self.client.close)

    def assert_error(self, kind, action):
        with self.assertRaises(LCUError) as result:
            action()
        self.assertEqual(result.exception.kind, kind)
        return result.exception

    def test_invalid_host_or_subject_never_constructs_a_network_session(self):
        for host, subject in (("127.0.0.1", SUBJECT), ("las-red.lol.sgp.pvp.net.evil", SUBJECT),
                              ("https://" + HOST, SUBJECT), (HOST + ":443", SUBJECT),
                              (HOST + "/path", SUBJECT), (HOST, "../other"), (HOST, None)):
            with self.subTest(host=host, subject=subject):
                factory = Mock()
                self.assert_error("data", lambda: TFTBackendClient(host, subject, self.provider, session_factory=factory))
                factory.assert_not_called()

    def test_reads_only_own_player_over_verified_https_without_proxies_redirects_or_retries(self):
        self.assertEqual(self.client.read_player(self.cancelled), player())
        self.factory.assert_called_once()
        self.assertIs(self.session.trust_env, False)
        self.assertIs(self.session.verify, True)
        self.assertEqual(self.session.proxies, {})
        self.assertIsNone(self.session.auth)
        self.assertNotIn("Authorization", self.session.headers)
        adapter = self.session.mount.call_args.args[1]
        self.assertEqual(adapter.max_retries.total, 0)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", f"https://{HOST}/parties-ledge/v1/players/{SUBJECT}"))
        self.assertEqual(kwargs["timeout"], HTTP_TIMEOUT)
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer opaque-own-gaps-token")

    def test_accept_validates_context_then_sends_exact_body_and_does_not_parse_empty_success(self):
        for status in (200, 204):
            with self.subTest(status=status):
                self.session.reset_mock()
                reply = response(status)
                reply.json.side_effect = AssertionError("No JSON on successful acceptance")
                self.session.request.side_effect = [response(), reply]
                self.client.accept(self.cancelled)
                self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["GET", "POST"])
                args, kwargs = self.session.request.call_args
                self.assertEqual(args[1], "https://" + HOST + "/team-builder-ledge/v3/indicateAfkReadiness")
                self.assertEqual(kwargs["json"], {
                    "additionalInventoryJWT": "", "afkReady": True, "initialSpellIds": [],
                    "lastSelectedSkinIdByChampionId": {}, "simplifiedInventoryJWT": "",
                })
                reply.json.assert_not_called()

    def test_same_token_is_used_for_context_validation_and_acceptance(self):
        self.provider.side_effect = ["own-session-one", "another-session"]
        self.client.accept(self.cancelled)
        self.provider.assert_called_once_with(self.cancelled)
        self.assertTrue(all(call.kwargs["headers"]["Authorization"] == "Bearer own-session-one"
                            for call in self.session.request.call_args_list))

    def test_cancelled_before_request_or_after_provider_never_uses_network(self):
        self.cancelled.set()
        self.assert_error("cancelled", lambda: self.client.accept(self.cancelled))
        self.provider.assert_not_called()
        self.cancelled.clear()
        def cancelled_token(cancelled):
            cancelled.set()
            return "opaque-token"
        self.provider.side_effect = cancelled_token
        self.assert_error("cancelled", lambda: self.client.accept(self.cancelled))
        self.session.request.assert_not_called()

    def test_cancel_after_context_get_prevents_post(self):
        def finish(*args, **kwargs):
            self.cancelled.set()
            return response()
        self.session.request.side_effect = finish
        self.assert_error("cancelled", lambda: self.client.accept(self.cancelled))
        self.assertEqual(self.session.request.call_count, 1)

    def test_cancel_during_post_or_timeout_takes_priority(self):
        for effect in (response(204), requests.Timeout("sensitive response")):
            with self.subTest(effect=type(effect).__name__):
                self.cancelled.clear()
                self.session.reset_mock()
                def finish(method, *args, **kwargs):
                    if method == "GET":
                        return response()
                    self.cancelled.set()
                    if isinstance(effect, Exception):
                        raise effect
                    return effect
                self.session.request.side_effect = finish
                self.assert_error("cancelled", lambda: self.client.accept(self.cancelled))
                self.assertEqual(self.session.request.call_count, 2)

    def test_statuses_are_classified_and_redirects_never_followed(self):
        for status, kind in ((401, "auth"), (403, "auth"), (404, "data"), (302, "http"), (500, "http")):
            with self.subTest(status=status):
                self.session.request.return_value = response(status)
                error = self.assert_error(kind, lambda: self.client.read_player(self.cancelled))
                self.assertEqual(error.status, status)

    def test_transport_errors_are_sanitized(self):
        for error, kind in ((requests.Timeout("secret"), "timeout"),
                            (requests.ConnectionError("secret"), "connection"),
                            (requests.exceptions.SSLError("secret"), "connection"),
                            (requests.RequestException("secret"), "http")):
            with self.subTest(kind=kind):
                self.session.request.side_effect = error
                result = self.assert_error(kind, lambda: self.client.read_player(self.cancelled))
                self.assertNotIn("secret", str(result))

    def test_timeout_post_is_not_retried_by_backend(self):
        self.session.request.side_effect = [response(), requests.Timeout("secret")]
        self.assert_error("timeout", lambda: self.client.accept(self.cancelled))
        self.assertEqual(self.session.request.call_count, 2)

    def test_manual_response_during_identity_read_prevents_post(self):
        pending = [True]
        def read(*args, **kwargs):
            pending[0] = False
            return response()
        self.session.request.side_effect = read
        self.assert_error("cancelled", lambda: self.client.accept(
            self.cancelled, before_post=lambda: pending[0]))
        self.assertEqual(self.session.request.call_count, 1)

    def test_wrong_account_or_game_context_prevents_acceptance(self):
        bad = player()
        bad["puuid"] = OTHER
        cases = [bad]
        bad = player()
        bad["currentParty"]["product"] = "lol"
        cases.append(bad)
        bad = player()
        bad["currentParty"]["players"] = [{"puuid": OTHER}]
        cases.append(bad)
        for data in cases:
            with self.subTest(data=data):
                self.session.reset_mock()
                self.session.request.return_value = response(data=data)
                self.assert_error("auth", lambda: self.client.accept(self.cancelled))
                self.assertEqual(self.session.request.call_count, 1)

    def test_rso_tft_membership_allows_acceptance_despite_lol_or_missing_current_party(self):
        for current_party in (rso_player()["currentParty"], None):
            with self.subTest(current_party=current_party):
                data = rso_player()
                data["currentParty"] = current_party
                self.session.reset_mock()
                self.session.request.side_effect = [response(data=data), response(204)]
                guard = Mock(return_value=True)
                self.client.accept(self.cancelled, before_post=guard)
                guard.assert_called_once()
                self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["GET", "POST"])
                # El validador no cambia la party de LoL por una TFT inventada.
                self.assertEqual(data["currentParty"], current_party)

    def test_rso_without_tft_membership_never_posts(self):
        data = rso_player()
        data["parties"] = data["parties"][:1]
        self.session.request.return_value = response(data=data)
        self.assert_error("auth", lambda: self.client.accept(self.cancelled))
        self.assertEqual(self.session.request.call_count, 1)

    def test_foreign_invalid_duplicate_or_oversized_memberships_never_post(self):
        cases = []
        for key, value, kind in (("puuid", OTHER, "auth"), ("puuid", "bad", "data"),
                                 ("partyId", "bad", "data"), ("partyProduct", None, "data")):
            data = rso_player()
            data["parties"][1][key] = value
            cases.append((data, kind))
        data = rso_player()
        data["parties"].append(dict(data["parties"][1]))
        cases.append((data, "data"))
        data = rso_player()
        data["parties"] *= MAX_MEMBERSHIPS
        cases.append((data, "data"))
        for data, kind in cases:
            with self.subTest(kind=kind):
                self.session.reset_mock()
                self.session.request.return_value = response(data=data)
                self.assert_error(kind, lambda: self.client.accept(self.cancelled))
                self.assertEqual(self.session.request.call_count, 1)

    def test_manual_response_during_rso_membership_check_still_prevents_post(self):
        pending = [True]
        def read(*args, **kwargs):
            pending[0] = False
            return response(data=rso_player())
        self.session.request.side_effect = read
        self.assert_error("cancelled", lambda: self.client.accept(
            self.cancelled, before_post=lambda: pending[0]))
        self.assertEqual(self.session.request.call_count, 1)

    def test_invalid_shapes_and_member_ids_are_rejected_without_post(self):
        cases = [[], {}, {"puuid": SUBJECT, "currentParty": None}]
        for key, value in (("partyId", "bad"), ("players", None), ("players", []),
                            ("players", ["bad"]), ("players", [{"name": "someone"}]),
                            ("players", [SUBJECT, SUBJECT])):
            data = player()
            data["currentParty"][key] = value
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.session.reset_mock()
                self.session.request.return_value = response(data=data)
                self.assert_error("data", lambda: self.client.accept(self.cancelled))
                self.assertEqual(self.session.request.call_count, 1)

    def test_json_failure_is_data_and_unsafe_tokens_never_reach_network(self):
        self.session.request.return_value.json.side_effect = ValueError("secret body")
        self.assert_error("data", lambda: self.client.read_player(self.cancelled))
        self.session.reset_mock()
        for token in (None, "", "secret\r\nheader", object()):
            with self.subTest(token_type=type(token).__name__):
                self.provider.return_value = token
                self.assert_error("auth", lambda: self.client.read_player(self.cancelled))
        self.session.request.assert_not_called()

    def test_close_is_idempotent_and_never_reopens_a_worker_session(self):
        self.client.read_player(self.cancelled)
        self.client.read_player(self.cancelled)
        self.factory.assert_called_once()
        self.client.close()
        self.client.close()
        self.session.close.assert_called_once()
        self.assert_error("connection", lambda: self.client.read_player(self.cancelled))
        self.factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
