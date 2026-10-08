#!/usr/bin/env python3
"""Opt-in live-model decision replay. All proposed tools stay disabled.

Results may contain private interaction data: retain them outside the public repo.
Run --live --output-dir PATH; network evaluation is NEVER part of pytest.
"""

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from drift_guard.next_safe_benchmark import SYSTEM, cases, current_template, input_text, parse_response, score  # noqa: E402


def evaluate(case, variant, sample, *, model, output, template):
    name = f"{variant}-{case['id']}-{sample}"
    prompt = input_text(case, variant, template)
    (output / f"{name}-input.txt").write_text(prompt)
    command = [
        shutil.which("pi"),
        "--print",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-themes",
        "--offline",
        "--model",
        model,
        "--thinking",
        "high",
        "--system-prompt",
        SYSTEM,
    ]
    started = time.monotonic()
    result = {"case": case["id"], "variant": variant, "sample": sample, "model": model}
    try:
        run = subprocess.run(command, cwd=output, input=prompt, capture_output=True, text=True, timeout=180, check=False)
        (output / f"{name}-output.txt").write_text(run.stdout)
        (output / f"{name}-stderr.txt").write_text(run.stderr)
        if run.returncode:
            result["error"] = f"provider/CLI exit {run.returncode}"
        else:
            response = parse_response(run.stdout)
            result.update(response=response, score=score(case, response))
    except (ValueError, subprocess.TimeoutExpired) as error:
        result["error"] = type(error).__name__
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Explicitly permit external model evaluation")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="openai-codex/gpt-6.1-sol")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--reference-template", type=Path, help="Frozen pre-fix prompt used for the current/reference arm")
    parser.add_argument(
        "--variants", nargs="+", choices=["baseline", "current", "candidate"], default=["baseline", "current", "candidate"]
    )
    args = parser.parse_args()
    if not args.live or not 1 <= args.repeats <= 5:
        parser.error("--live required; repeats must be 1..5")
    output = args.output_dir.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("retain potentially private replay outputs outside the source repository")
    output.mkdir(parents=True, exist_ok=False)
    candidate_template = current_template()
    template = args.reference_template.read_text() if args.reference_template else candidate_template
    fixture_cases = cases()
    (output / "current-template.txt").write_text(template)
    (output / "candidate-template.txt").write_text(candidate_template)
    (output / "fixtures.json").write_text(json.dumps(fixture_cases, indent=2))
    jobs = [
        (case, variant, sample) for variant in args.variants for case in fixture_cases for sample in range(1, args.repeats + 1)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                evaluate,
                case,
                variant,
                sample,
                model=args.model,
                output=output,
                template=candidate_template if variant == "candidate" else template,
            )
            for case, variant, sample in jobs
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    results.sort(key=lambda row: (row["variant"], row["case"], row["sample"]))
    (output / "results.json").write_text(json.dumps(results, indent=2))
    summary = {
        "model": args.model,
        "repeats": args.repeats,
        "fixture_count": len(fixture_cases),
        "fixture_sha256": hashlib.sha256(json.dumps(fixture_cases, sort_keys=True).encode()).hexdigest(),
        "current_template_sha256": hashlib.sha256(template.encode()).hexdigest(),
        "candidate_template_sha256": hashlib.sha256(candidate_template.encode()).hexdigest(),
        "rubric_version": "3: valid tools, visible concise quotations, and no stopping with explicit parent-owned work",
        "historical_live_failure": "case07 stopped after suggesting a benchmark; explicitly rejected by the user",
        "variants": {},
    }
    for variant in args.variants:
        rows = [row for row in results if row["variant"] == variant]
        summary["variants"][variant] = {
            "samples": len(rows),
            "action_pass": sum(row.get("score", {}).get("action_pass", False) for row in rows),
            "full_rubric_pass": sum(row.get("score", {}).get("pass", False) for row in rows),
            "malformed_or_provider_errors": sum("error" in row for row in rows),
            "failures": [
                {
                    "case": row["case"],
                    "sample": row["sample"],
                    "action": row.get("response", {}).get("action"),
                    "error": row.get("error"),
                }
                for row in rows
                if not row.get("score", {}).get("pass")
            ],
        }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
