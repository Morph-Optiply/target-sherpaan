"""Offline regression tests for the SOAP client's Tenacity policy."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from requests import Response
from requests.exceptions import ConnectionError, HTTPError, Timeout
from tenacity import RetryError

from target_sherpaan.client import SherpaClient


LOCK = "Purchase order is locked"
ENVELOPE = '<Envelope><PurchaseOrder>synthetic-PO-é</PurchaseOrder></Envelope>'


def response(status, body):
    result = Response()
    result.status_code = status
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    return result


def fault(message):
    return response(500, (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><soap:Fault><detail><Error><ErrorMessage>'
        f'{escape(message)}</ErrorMessage></Error></detail></soap:Fault>'
        '</soap:Body></soap:Envelope>'
    ))


def success():
    return response(200, (
        '<Envelope><Body><ChangePurchase2Response><Result><Accepted>true</Accepted>'
        '</Result></ChangePurchase2Response></Body></Envelope>'
    ))


class SoapRetryTest(unittest.TestCase):
    def setUp(self):
        self.client = SherpaClient(SimpleNamespace(base_url="https://example.invalid/Sherpa.asmx"))
        self.addCleanup(self.client.session.close)
        self.client.logger = Mock()
        self.post = self.client.session.post = Mock()
        self.sleeps = []
        # copy uses the real installed Tenacity controller without actually sleeping.
        self.call = self.client.call_soap_service.retry_with(sleep=self.sleeps.append)

    def invoke(self, outcomes, service="ChangePurchase2", failure=None):
        self.post.side_effect = outcomes
        if failure is None:
            result = self.call(self.client, service, ENVELOPE)
            self.assertEqual(result, {"Accepted": "true"})
            error = None
        else:
            with self.assertRaises(RetryError) as caught:
                self.call(self.client, service_name=service, soap_envelope=ENVELOPE)
            error = caught.exception.last_attempt.exception()
            self.assertIsInstance(error, failure)
        self.assertEqual(self.post.call_count, len(outcomes))
        for request in self.post.call_args_list:
            self.assertEqual(request.kwargs["data"], ENVELOPE.encode("utf-8"))
        self.assertEqual(
            self.client.session.headers["SOAPAction"],
            f'"http://sherpa.sherpaan.nl/{service}"',
        )
        return error

    def assert_lock_waits(self, bases):
        self.assertEqual(len(self.sleeps), len(bases))
        for delay, base in zip(self.sleeps, bases):
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 1)

    def test_lock_then_success(self):
        self.invoke([fault(LOCK), success()])
        self.assert_lock_waits([5])

    def test_permanent_lock_preserves_final_fault(self):
        error = self.invoke([fault(LOCK) for _ in range(5)], failure=ValueError)
        self.assertEqual(str(error), f"SOAP Fault from ChangePurchase2: {LOCK}")
        self.assert_lock_waits([5, 10, 20, 40])

    def test_other_soap_faults_keep_old_policy(self):
        for message in ("Unknown item code SYNTHETIC", "Permission denied", LOCK + "."):
            with self.subTest(message=message):
                self.post.reset_mock()
                self.sleeps.clear()
                error = self.invoke([fault(message) for _ in range(3)], failure=ValueError)
                self.assertEqual(str(error), f"SOAP Fault from ChangePurchase2: {message}")
                self.assertEqual(self.sleeps, [4, 4])

    def test_creation_service_lock_keeps_old_policy(self):
        service = "AddOrderedPurchaseWithExternalOrderNumber"
        error = self.invoke([fault(LOCK) for _ in range(3)], service, ValueError)
        self.assertEqual(str(error), f"SOAP Fault from {service}: {LOCK}")
        self.assertEqual(self.sleeps, [4, 4])

    def test_http_error_keeps_old_policy(self):
        self.invoke([response(500, "Internal server error") for _ in range(3)], failure=HTTPError)
        self.assertEqual(self.sleeps, [4, 4])

    def test_transport_errors_keep_old_policy(self):
        for error_type in (ConnectionError, Timeout):
            with self.subTest(error_type=error_type):
                self.post.reset_mock()
                self.sleeps.clear()
                final = self.invoke([error_type("synthetic failure") for _ in range(3)], failure=error_type)
                self.assertEqual(str(final), "synthetic failure")
                self.assertEqual(self.sleeps, [4, 4])

    def test_mixed_faults_use_total_attempt_budget(self):
        self.invoke([fault("Unknown item code SYNTHETIC"), fault(LOCK), fault(LOCK),
                     fault(LOCK), fault(LOCK)], failure=ValueError)
        self.assertEqual(self.sleeps[0], 4)
        for delay, base in zip(self.sleeps[1:], [10, 20, 40]):
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 1)
        self.assertEqual(len(self.sleeps), 4)

    def test_nonlock_after_third_attempt_stops_immediately(self):
        error = self.invoke([fault(LOCK), fault(LOCK), fault(LOCK),
                             fault("Unknown item code SYNTHETIC")], failure=ValueError)
        self.assertEqual(str(error), "SOAP Fault from ChangePurchase2: Unknown item code SYNTHETIC")
        self.assert_lock_waits([5, 10, 20])

    def test_jitter_is_additive_at_both_bounds(self):
        for jitter in (0, 1):
            with self.subTest(jitter=jitter):
                self.post.reset_mock()
                self.sleeps.clear()
                with patch("random.uniform", return_value=jitter):
                    self.invoke([fault(LOCK) for _ in range(5)], failure=ValueError)
                self.assertEqual(self.sleeps, [base + jitter for base in (5, 10, 20, 40)])


if __name__ == "__main__":
    unittest.main()
