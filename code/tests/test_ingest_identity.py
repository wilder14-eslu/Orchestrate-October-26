"""Unit tests for ingestion rules and identity resolution (stdlib unittest).

Run from the repository root:  python -m unittest discover -s code/tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.identity import IdentityIndex, email_hash  # noqa: E402
from agent.ingest import json_records, log_events, sql_snapshot  # noqa: E402
from agent.ingest.pdf_text import ocr_vote  # noqa: E402
from agent.ingest.records import RecordContext, interpret  # noqa: E402
from agent.models import AuditChange, Identity, ManifestEntry, Request  # noqa: E402

ENTRY = ManifestEntry("evidence_t", "evidence/test", "unit_test", "2026-10-01T12:00:00Z")
KEY = "44a65154bfd8e408852578fbd76813d094d1944f255ac1769ee81ba90492f10e"


def log_obs(line: str):
    return log_events.parse(ENTRY, line, [])[0]


class MatchedKeyRule(unittest.TestCase):
    def test_no_identifier_field_printed_means_identifier_is_held(self):
        # Opt-in contact row matched by an old email hash: holds the identifier, categorized as a marketing contact.
        (obs,) = log_obs(f"2026-09-15T11:58:26Z level=DEBUG service=analytics_platform trace_id=t1 resource_id=analytics_1 "
                         f"event=contact_lookup_completed rows=1 email_opt_in=true correlation_key=\"{KEY}\"")
        self.assertIn("matched_key", obs.pii_fields)
        self.assertEqual((obs.category, obs.presence), ("MARKETING_PROFILE", "PRESENT"))
        # Analytics profile matched by the same key with no identifier columns printed: identifiable.
        (obs,) = log_obs(f"2026-09-15T11:58:30Z level=INFO service=analytics_platform trace_id=t3 resource_id=analytics_3 "
                         f"event=analytics_profile_read rows=1 correlation_key=\"{KEY}\"")
        self.assertEqual(obs.observed_state, "IDENTIFIABLE")

    def test_printed_null_fields_mean_removed(self):
        lines = ("2026-09-26T08:57:38Z level=DEBUG service=analytics_platform trace_id=t2 event=analytics_profile_read_started "
                 "resource_id=analytics_2 lookup_key=someone@example.com\n"
                 "2026-09-26T08:57:38Z level=INFO service=analytics_platform trace_id=t2 event=analytics_profile_read_completed "
                 "resource_id=analytics_2 customer_reference=null email=null phone=null email_hash=null audience_cohort=x")
        (obs,) = log_obs(lines)
        self.assertEqual(obs.observed_state, "ANONYMIZED")
        self.assertEqual(obs.pii_fields, frozenset())


class Formats(unittest.TestCase):
    def test_six_log_line_styles_join_lookup_key_to_resource(self):
        styles = [
            '2026-09-10T08:56:17Z {"service":"account_service","trace_id":"j1","event":"profile_lookup_started","lookup_key":"a@x.io"}\n'
            '2026-09-10T08:56:20Z {"service":"account_service","trace_id":"j1","resource_id":"account_1","rows":"0","http_status":"404"}',
            '2026-08-22T10:51:11Z <15>1 node-1 account_service tr-1 - - level=DEBUG event="profile_lookup_started" lookup_key="a@x.io"\n'
            '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-1 - - level=DEBUG resource_id="account_1" rows="0" http_status="404"',
            '2026-06-08T06:52:09Z requestId=tr-2 level=INFO function=account_service message="event=\\"x\\" lookup_key=\\"a@x.io\\""\n'
            '2026-06-08T06:52:10Z requestId=tr-2 level=INFO function=account_service message="resource_id=\\"account_1\\" http_status=\\"404\\""',
            '2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-3] profile_lookup_started: lookup_key="a@x.io"\n'
            '2026-07-28T05:51:21Z [INFO] [account_service] [tr-3] profile_lookup_completed: resource_id="account_1" rows="0" http_status="404"',
            '2026-07-03T07:51:49Z | DEBUG | account_service | tr-4 | profile_lookup_started | lookup_key="a@x.io"\n'
            '2026-07-03T07:51:59Z | INFO | account_service | tr-4 | profile_lookup_completed | resource_id="account_1" http_status="404"',
            '2026-09-26T07:49:50Z level=DEBUG service=account_service trace_id=rb-1 event=profile_lookup_started resource_id=account_1 lookup_key=a@x.io\n'
            '2026-09-26T07:49:50Z level=INFO service=account_service trace_id=rb-1 event=profile_lookup_completed resource_id=account_1 rows=0 http_status=404',
        ]
        for text in styles:
            with self.subTest(text=text[:40]):
                (obs,) = log_obs(text)
                self.assertEqual((obs.resource_id, obs.presence), ("account_1", "ABSENT_DIRECT"))
                self.assertIn("a@x.io", obs.identifiers.emails)

    def test_sql_recovers_rows_after_a_malformed_statement(self):
        sql = ("CREATE TABLE security_audit_records (resource_id TEXT PRIMARY KEY, subject_reference TEXT);\n"
               "INSERT INTO security_audit_records VALUES ('audit_record_1', 'user_1');\n"
               "INSERT INTO security_audit_records VALUES ('audit_record_2', 'unterminated\n"
               "INSERT INTO security_audit_records VALUES ('audit_record_3', 'user_3');\n")
        warnings = []
        observations, _ = sql_snapshot.parse(ENTRY, sql, warnings)
        self.assertEqual(sorted(o.resource_id for o in observations), ["audit_record_1", "audit_record_3"])
        self.assertTrue(any("incomplete SQL statement" in w.message for w in warnings))

    def test_truncated_json_is_salvaged_with_lower_confidence(self):
        text = ('{"snapshots": [{"data_store": "search_service", "records": ['
                '{"resource_id": "search_1", "email": "a@x.io"}, {"resource_id": "search_2", "ema')
        warnings = []
        observations, scopes = json_records.parse(ENTRY, text, warnings)
        self.assertEqual([o.resource_id for o in observations], ["search_1"])
        self.assertEqual(observations[0].extraction_method, "salvage")
        self.assertLess(observations[0].confidence, 1.0)
        self.assertEqual(scopes, [])  # a truncated listing never proves absence by omission
        self.assertTrue(warnings and "char" in warnings[0].offset)

    def test_ocr_vote_drops_email_misreads_but_keeps_sibling_rows(self):
        text = ("support_9538 a@icloud.com\nsupport_9538 a@icloud.com\nsupport_9538 a@icloud.cor\nsupport_9638\n"
                "audit_record_001\naudit_record_002\n")
        cleaned, suspects = ocr_vote(text)
        self.assertNotIn("icloud.cor", cleaned)
        self.assertIn("support_9638", cleaned)          # never deleted, only flagged
        self.assertEqual(suspects, {"support_9638"})
        self.assertIn("audit_record_002", cleaned)


class IdentityResolution(unittest.TestCase):
    def setUp(self):
        ids = [Identity("user_1", "Ana Paz", "ana.new@x.io", "+51 900 000 001", email_hash("ana.new@x.io"), "2026-05-01T00:00:00Z")]
        log = [AuditChange("audit_1", "user_1", "2026-05-01T00:00:00Z", "email", "ana.old@x.io", "ana.new@x.io"),
               AuditChange("audit_2", "user_1", "2026-05-01T00:00:00Z", "email_hash", email_hash("ana.old@x.io"), email_hash("ana.new@x.io"))]
        self.index = IdentityIndex(ids, log)

    def request(self, email):
        return Request("case_x", email, "2026-09-01T00:00:00Z", "COMPLETE", "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z", "delete")

    def test_historical_email_resolves_to_owner(self):
        res = self.index.resolve(self.request("ANA.OLD@x.io"))
        self.assertEqual((res.user_id, res.via), ("user_1", "historical"))

    def test_profile_keeps_old_hash_with_validity(self):
        profile = self.index.profile("user_1", "2026-09-03T00:00:00Z")
        self.assertIn(email_hash("ana.old@x.io"), profile.identifiers().hashes)
        self.assertIn(email_hash("ana.old@x.io"), profile.historical())

    def test_unknown_email_is_unresolved(self):
        self.assertEqual(self.index.resolve(self.request("nobody@x.io")).via, "unresolved")


if __name__ == "__main__":
    unittest.main()
