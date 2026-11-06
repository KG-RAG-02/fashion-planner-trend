import unittest

from trend_agent.schemas import trend_result_from_mapping, trend_result_to_mapping


class TrendResultSchemaTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "result": {
                "agent_id": "query-trend",
                "request_id": "req-001",
                "schema_version": "internal-v1",
                "status": "ok",
                "evidence_ids": ["query-trend:E-001", "query-trend:E-002"],
                "limitations": ["기사 관찰 범위 내 결과"],
            },
            "candidates": [
                {
                    "name": "절제된 맥시멀리스트",
                    "qualification": "candidate",
                    "season": "2027SS",
                    "category": "women_knit",
                    "attributes": [
                        {
                            "name": "color",
                            "value": "deep burgundy",
                            "origin": "evidence_observation",
                            "evidence_ids": ["query-trend:E-001"],
                        },
                        {
                            "name": "pattern",
                            "value": "oversized floral",
                            "origin": "evidence_observation",
                            "evidence_ids": ["query-trend:E-002"],
                        },
                    ],
                    "evidence_ids": ["query-trend:E-001", "query-trend:E-002"],
                }
            ],
        }

    def test_preserves_name_dimensions_and_per_attribute_evidence(self):
        result = trend_result_from_mapping(self.payload)
        candidate = result.candidates[0]

        self.assertEqual(candidate.name, "절제된 맥시멀리스트")
        self.assertEqual((candidate.season, candidate.category), ("2027SS", "women_knit"))
        self.assertEqual(candidate.attributes[0].evidence_ids, ("query-trend:E-001",))
        self.assertEqual(candidate.attributes[1].evidence_ids, ("query-trend:E-002",))
        self.assertEqual(result.result.evidence_ids, ("query-trend:E-001", "query-trend:E-002"))

    def test_round_trip_keeps_status_limitations_and_attribute_lineage(self):
        result = trend_result_from_mapping(self.payload)
        serialized = trend_result_to_mapping(result)

        self.assertEqual(serialized["result"]["status"], "ok")
        self.assertEqual(serialized["result"]["limitations"], ("기사 관찰 범위 내 결과",))
        self.assertEqual(serialized["candidates"][0]["attributes"][1]["evidence_ids"], ("query-trend:E-002",))
        self.assertEqual(serialized["candidates"][0]["name"], "절제된 맥시멀리스트")

    def test_non_ok_statuses_and_missing_evidence_do_not_synthesize_trend_content(self):
        for status in ("partial", "no_results", "error"):
            with self.subTest(status=status):
                payload = {
                    "result": {
                        "agent_id": "query-trend",
                        "request_id": "req-001",
                        "schema_version": "internal-v1",
                        "status": status,
                        "limitations": ["속성 근거 부족"],
                    },
                    "candidates": [],
                }
                result = trend_result_from_mapping(payload)
                self.assertEqual(result.result.status, status)
                self.assertEqual(result.candidates, ())

        no_attribute_evidence = {**self.payload, "candidates": [
            {**self.payload["candidates"][0], "attributes": [
                {"name": "material", "value": "unknown", "origin": "unknown"}
            ]}
        ]}
        result = trend_result_from_mapping(no_attribute_evidence)
        attribute = result.candidates[0].attributes[0]
        self.assertEqual(attribute.value, "unknown")
        self.assertEqual(attribute.evidence_ids, ())

    def test_rejects_unknown_status_and_malformed_evidence_ids(self):
        invalid_status = {**self.payload, "result": {**self.payload["result"], "status": "unknown"}}
        with self.assertRaisesRegex(ValueError, "result.status"):
            trend_result_from_mapping(invalid_status)

        bad_evidence = {**self.payload, "candidates": [
            {**self.payload["candidates"][0], "attributes": [
                {**self.payload["candidates"][0]["attributes"][0], "evidence_ids": [123]}
            ]}
        ]}
        with self.assertRaisesRegex(ValueError, "evidence_ids"):
            trend_result_from_mapping(bad_evidence)


if __name__ == "__main__":
    unittest.main()
