"""Run a bounded Claude plus Icarus Verilog evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from siliconloop import as_dict, extract_top_module, judge, load_problems, pass_rate


def prompt_for_initial(problem_prompt: str, trial: int) -> str:
    return f"""You are the RTL implementation agent in SiliconLoop, trial {trial}.
Write exactly one standalone SystemVerilog module named TopModule that satisfies the English specification below.
Return only compilable source code, with no Markdown fences and no explanation.
Do not write a testbench, reference module, or wrapper. Use only the ports required by the specification.

English specification:
{problem_prompt}
"""


def prompt_for_retry(problem_prompt: str, candidate: str, feedback: str, retry_number: int, trial: int) -> str:
    return f"""You are repairing your RTL implementation in SiliconLoop, trial {trial}, retry {retry_number}.
Return exactly one complete standalone SystemVerilog module named TopModule and nothing else.
The original English specification is below. The simulator feedback is the only judge evidence you may use.
Do not write a testbench, reference module, or wrapper.

English specification:
{problem_prompt}

Your previous attempt:
{candidate}

Icarus Verilog simulator feedback:
{feedback[:6000]}
"""


class ClaudeAdapter:
    def __init__(self, model: str, cache_dir: Path, max_calls: int = 140):
        self.model = model
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.actual_calls = 0
        self.cached_calls = 0
        self.max_calls = max_calls

    def call(self, prompt: str) -> tuple[str, bool]:
        key = hashlib.sha256((self.model + "\n" + prompt).encode()).hexdigest()
        cache_path = self.cache_dir / f"{key}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text())
            self.cached_calls += 1
            return payload["result"], True
        if self.actual_calls >= self.max_calls:
            raise RuntimeError("LLM call budget guard reached before the evaluation completed")
        completed = subprocess.run(
            ["claude", "-p", "--model", self.model, "--output-format", "json", prompt],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.actual_calls += 1
        if completed.returncode != 0:
            raise RuntimeError(f"Claude CLI failed with exit {completed.returncode}: {completed.stderr[-1000:]}")
        try:
            payload = json.loads(completed.stdout)
            result = payload["result"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(f"Claude CLI did not return the documented JSON result field: {exc}") from exc
        cache_path.write_text(json.dumps({"model": self.model, "result": result}, indent=2) + "\n")
        return result, False


def run_trial(adapter: ClaudeAdapter, problem, trial: int, max_retries: int) -> dict:
    started = time.perf_counter()
    initial_prompt = prompt_for_initial(problem.prompt, trial)
    initial_text, initial_cached = adapter.call(initial_prompt)
    first_candidate = extract_top_module(initial_text)
    first_judgement = judge(problem, first_candidate)
    attempts = [{"attempt": 0, "candidate": first_candidate, "judge": as_dict(first_judgement)}]
    current_candidate = first_candidate
    final_judgement = first_judgement
    retries_used = 0
    while final_judgement.status != "pass" and retries_used < max_retries:
        retries_used += 1
        retry_prompt = prompt_for_retry(problem.prompt, current_candidate, final_judgement.output, retries_used, trial)
        retry_text, _ = adapter.call(retry_prompt)
        current_candidate = extract_top_module(retry_text)
        final_judgement = judge(problem, current_candidate)
        attempts.append({"attempt": retries_used, "candidate": current_candidate, "judge": as_dict(final_judgement)})
    return {
        "problem_id": problem.problem_id,
        "title": problem.title,
        "trial": trial,
        "one_shot_status": first_judgement.status,
        "looped_status": final_judgement.status,
        "retry_count": retries_used,
        "fixed_by_retry": first_judgement.status != "pass" and final_judgement.status == "pass",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "initial_response_cached": initial_cached,
        "attempts": attempts,
    }


def build_controlled_replay(problem, row: dict) -> dict:
    """Create a truthful demo replay when the pilot has no natural retry fix."""
    candidate = row["attempts"][0]["candidate"]
    wrong_candidate = candidate.replace("1'b0", "1'b1", 1)
    if wrong_candidate == candidate:
        raise RuntimeError("The controlled replay fixture could not perturb the Prob001 candidate")
    wrong_judgement = judge(problem, wrong_candidate)
    fixed_judgement = judge(problem, candidate)
    if wrong_judgement.status == "pass" or fixed_judgement.status != "pass":
        raise RuntimeError("The controlled replay fixture did not produce wrong_output then pass")
    return {
        "problem_id": problem.problem_id,
        "title": problem.title,
        "trial": row["trial"],
        "one_shot_status": wrong_judgement.status,
        "looped_status": fixed_judgement.status,
        "retry_count": 1,
        "fixed_by_retry": True,
        "controlled_replay": True,
        "replay_note": "The first module is a deliberate one-line failure fixture. The second is the recorded Claude candidate.",
        "prompt": problem.prompt,
        "attempts": [
            {"attempt": 0, "candidate": wrong_candidate, "judge": as_dict(wrong_judgement)},
            {"attempt": 1, "candidate": candidate, "judge": as_dict(fixed_judgement)},
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SiliconLoop's bounded agentic Verilog experiment.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/verilog_eval"))
    parser.add_argument("--output", type=Path, default=Path("site/data/results.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/claude_cache"))
    parser.add_argument("--problems", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--model", default="claude-haiku-5-5")
    args = parser.parse_args()

    problems = load_problems(args.data_dir)
    if not problems:
        raise SystemExit(f"No complete VerilogEval problems found in {args.data_dir}")
    selected = problems[: args.problems]
    adapter = ClaudeAdapter(args.model, args.cache_dir)
    rows = []
    for trial in range(1, args.repeats + 1):
        for problem in selected:
            print(f"trial={trial} problem={problem.problem_id}", flush=True)
            rows.append(run_trial(adapter, problem, trial, args.retries))

    one_shot = [{"status": row["one_shot_status"]} for row in rows]
    looped = [{"status": row["looped_status"]} for row in rows]
    one_shot_breakdown = Counter(row["one_shot_status"] for row in rows)
    looped_breakdown = Counter(row["looped_status"] for row in rows)
    replay = next((row for row in rows if row["fixed_by_retry"]), None)
    if replay is None:
        replay = build_controlled_replay(selected[0], rows[0])
    elif "prompt" not in replay:
        replay["prompt"] = next(problem.prompt for problem in selected if problem.problem_id == replay["problem_id"])
    output = {
        "project": "SiliconLoop",
        "recorded_with": f"Claude Haiku 5.5 via Claude Code, {datetime.now().date().isoformat()}",
        "model": args.model,
        "dataset": "NVlabs VerilogEval dataset_spec-to-rtl",
        "dataset_target_problems": 156,
        "pilot_problems_per_trial": len(selected),
        "repeats": args.repeats,
        "max_retries": args.retries,
        "one_shot_pass_rate": pass_rate(one_shot),
        "looped_pass_rate": pass_rate(looped),
        "one_shot_passes": sum(row["status"] == "pass" for row in one_shot),
        "looped_passes": sum(row["status"] == "pass" for row in looped),
        "evaluated_attempts": len(rows),
        "fixed_by_retries": sum(row["fixed_by_retry"] for row in rows),
        "one_shot_failure_breakdown": dict(sorted(one_shot_breakdown.items())),
        "looped_failure_breakdown": dict(sorted(looped_breakdown.items())),
        "mean_seconds_per_problem": round(sum(row["elapsed_seconds"] for row in rows) / len(rows), 3),
        "direct_api_cost_usd": 0,
        "llm_calls_made": adapter.actual_calls + adapter.cached_calls,
        "llm_calls_executed": adapter.actual_calls,
        "llm_cache_hits": adapter.cached_calls,
        "rows": rows,
        "replay": replay,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({key: output[key] for key in ("one_shot_pass_rate", "looped_pass_rate", "fixed_by_retries", "llm_calls_made", "mean_seconds_per_problem")}, indent=2))


if __name__ == "__main__":
    main()
