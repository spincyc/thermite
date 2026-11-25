"""The cap on one peer's event streams and the alert for a peer that holds
too many of them over HTTP/1.1 are two constants in two packages. The alert
watches a count the cap bounds, so the cap must never drop below it."""

import re

from django.test import SimpleTestCase

from oh_access import metrics
from oh_identity import streams
from oh_metrics import alerts


class StreamCapAndStarvedAlert(SimpleTestCase):
    def rule(self):
        [rule] = [rule for group in alerts.rules({})["groups"] for rule in group["rules"]
                  if rule.get("alert") == "ThermiteRemoteStarved"]
        return rule

    def test_the_alert_reads_the_most_http11_streams_one_peer_holds_against_the_browsers_limit(self):
        gauge = next(family.name for family in metrics.SSE_PEER_MAX.collect())
        watched = re.fullmatch(re.escape(gauge) + r'\{http_version="([^"]+)"\} >= (\d+)', self.rule()["expr"])
        self.assertIsNotNone(watched, self.rule()["expr"])
        self.assertEqual((watched[1], int(watched[2])), ("1.1", alerts.HTTP1_CONNECTIONS))
        self.assertIn(watched[1], metrics.HTTP_VERSIONS)

    def test_a_peer_is_admitted_the_streams_that_fire_the_alert(self):
        # The cap is the most the gauge can ever show for one peer. Were it
        # below the alert's threshold, the stream that starves a browser's
        # HTTP/1.1 connections would be refused and the alert could not fire.
        self.assertLessEqual(alerts.HTTP1_CONNECTIONS, streams.PEER_STREAMS)
        slots, fields = streams.Slots(), {"peer": "digest-of-one-peer", "view": "remote", "http_version": "1.1"}
        self.assertEqual(slots.per_peer, streams.PEER_STREAMS)
        releases = []
        try:
            while (acquired := slots.acquire("192.0.2.1"))[0] is not None:
                releases.append(acquired[0])
                metrics.observe("stream_open", fields)
            self.assertEqual(acquired, (None, "peer"))
            metrics.refresh_streams()
            held = metrics.SSE_PEER_MAX.labels("1.1")._value.get()
        finally:
            for release in releases:
                metrics.observe("stream_close", {**fields, "seconds": 0, "reason": "client"})
                release()
            metrics.refresh_streams()
        self.assertEqual((len(releases), held), (streams.PEER_STREAMS, streams.PEER_STREAMS))
        self.assertGreaterEqual(held, alerts.HTTP1_CONNECTIONS)
