# Example records

`synthetic-input.jsonl` and `synthetic-output.jsonl` provide a deliberately synthetic example pair demonstrating the exact JSONL shape emitted by the agent. The example output uses `local_test_fixture` evidence and an `example.invalid` URL. It exercises schema conformance, structured fact-level evidence, citations (`[ev-...]`), and operations telemetry. It is not company data and must not be submitted as a real company profile. The output envelope strictly matches the official Builderr evaluation contract (`OUTPUT_CONTRACT.md` and `schema/output-envelope.schema.json`).
