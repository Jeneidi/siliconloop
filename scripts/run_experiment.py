"""Run a bounded Claude plus Icarus Verilog evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from siliconloop import as_dict, extract_top_module, judge, load_problems, pass_rate, patch_icarus13_testbench


SELECTION_RULE = (
    "Compatible problems are ranked before any model call by score = 100 * FSM indicator "
    "+ 50 * sequential indicator + 10 * temporal/state keyword count + prompt line count "
    "+ 0.5 * reference line count + 0.25 * testbench line count. Ties use problem ID. "
    "FSM indicators are state machine, FSM, Moore, Mealy, or transition table. Sequential "
    "indicators are posedge, clock, reset, sequential, flip-flop, latch, counter, or timer."
)
FSM_TERMS = ("state machine", "fsm", "moore", "mealy", "transition table")
SEQUENTIAL_TERMS = ("posedge", "clock", "reset", "sequential", "flip-flop", "latch", "counter", "timer")
TEMPORAL_TERMS = ("state", "cycle", "count", "edge", "shift", "delay", "sequence", "ack")


def prompt_for_initial(problem_prompt: str) -> str:
    return f"""You are the RTL implementation agent in SiliconLoop.
Write exactly one standalone SystemVerilog module named TopModule that satisfies the English specification below.
Return only compilable source code, with no Markdown fences and no explanation.
Do not write a testbench, reference module, or wrapper. Use only the ports required by the specification.

English specification:
{problem_prompt}
"""


def prompt_for_retry(problem_prompt: str, candidate: str, feedback: str, retry_number: int) -> str:
    return f"""You are repairing your RTL implementation in SiliconLoop, retry {retry_number}.
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


def _named_top_ports(testbench: str) -> list[str]:
    match = re.search(r"TopModule\s+top_module1\s*\((.*?)\);", testbench, re.DOTALL)
    if not match:
        return []
    return sorted(set(re.findall(r"\.([A-Za-z_]\w*)\s*(?:\(|,|$)", match.group(1))))


def compatibility_check(problem) -> tuple[bool, str]:
    """Compile the official reference and testbench with a harmless TopModule stub."""
    testbench = patch_icarus13_testbench(problem.test_path.read_text())
    ports = _named_top_ports(testbench)
    if not ports:
        return False, "Could not identify named TopModule ports in the official testbench"
    declarations = ", ".join(f"input wire {port}" for port in ports)
    stub = f"module TopModule({declarations}); endmodule\n"
    with tempfile.TemporaryDirectory(prefix="siliconloop-compat-") as scratch:
        scratch_path = Path(scratch)
        ref_path = scratch_path / "reference.sv"
        test_path = scratch_path / "test.sv"
        stub_path = scratch_path / "stub.sv"
        output_path = scratch_path / "compat.out"
        ref_path.write_text(problem.reference_path.read_text())
        test_path.write_text(testbench)
        stub_path.write_text(stub)
        result = subprocess.run(
            ["/opt/homebrew/bin/iverilog", "-g2012", "-o", str(output_path), str(stub_path), str(ref_path), str(test_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def metadata_score(problem) -> dict:
    prompt = problem.prompt.lower()
    reference = problem.reference_path.read_text()
    testbench = problem.test_path.read_text()
    fsm_hits = sum(prompt.count(term) for term in FSM_TERMS)
    sequential_hits = sum(prompt.count(term) for term in SEQUENTIAL_TERMS)
    temporal_hits = sum(prompt.count(term) for term in TEMPORAL_TERMS)
    fsm_indicator = int(fsm_hits > 0)
    sequential_indicator = int(sequential_hits > 0)
    score = round(
        100 * fsm_indicator
        + 50 * sequential_indicator
        + 10 * temporal_hits
        + len(problem.prompt.splitlines())
        + 0.5 * len(reference.splitlines())
        + 0.25 * len(testbench.splitlines()),
        2,
    )
    return {
        "problem_id": problem.problem_id,
        "title": problem.title,
        "score": score,
        "fsm_indicator": fsm_indicator,
        "sequential_indicator": sequential_indicator,
        "temporal_keyword_count": temporal_hits,
        "prompt_lines": len(problem.prompt.splitlines()),
        "reference_lines": len(reference.splitlines()),
        "testbench_lines": len(testbench.splitlines()),
    }


def select_hardest(problems: list, count: int) -> tuple[list, list[dict], list[dict]]:
    compatible = []
    excluded = []
    for problem in problems:
        ok, output = compatibility_check(problem)
        if ok:
            compatible.append(problem)
        else:
            excluded.append({"problem_id": problem.problem_id, "title": problem.title, "reason": output})
    scored = [metadata_score(problem) for problem in compatible]
    scores_by_id = {row["problem_id"]: row for row in scored}
    ranked = sorted(compatible, key=lambda problem: (-scores_by_id[problem.problem_id]["score"], problem.problem_id))
    selected = ranked[:count]
    return selected, [scores_by_id[problem.problem_id] for problem in selected], excluded


class ClaudeAdapter:
    def __init__(self, model: str, cache_dir: Path, max_new_calls: int = 130):
        self.model = model
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.actual_calls = 0
        self.cached_calls = 0
        self.max_new_calls = max_new_calls

    def can_make_new_call(self) -> bool:
        return self.actual_calls < self.max_new_calls

    def call(self, prompt: str) -> tuple[str, bool]:
        key = hashlib.sha256((self.model + "\n" + prompt).encode()).hexdigest()
        cache_path = self.cache_dir / f"{key}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text())
            self.cached_calls += 1
            return payload["result"], True
        if not self.can_make_new_call():
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


def initial_row(adapter: ClaudeAdapter, problem) -> dict:
    started = time.perf_counter()
    initial_text, initial_cached = adapter.call(prompt_for_initial(problem.prompt))
    first_candidate = extract_top_module(initial_text)
    first_judgement = judge(problem, first_candidate)
    return {
        "problem_id": problem.problem_id,
        "title": problem.title,
        "one_shot_status": first_judgement.status,
        "looped_status": first_judgement.status,
        "retry_count": 0,
        "fixed_by_retry": False,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "initial_response_cached": initial_cached,
        "attempts": [{"attempt": 0, "candidate": first_candidate, "judge": as_dict(first_judgement)}],
    }


def apply_feedback_loop(adapter: ClaudeAdapter, problem_by_id: dict, rows: list[dict], max_retries: int) -> None:
    for row in rows:
        if row["one_shot_status"] == "pass":
            continue
        problem = problem_by_id[row["problem_id"]]
        current_candidate = row["attempts"][0]["candidate"]
        final_judgement = row["attempts"][0]["judge"]
        while final_judgement["status"] != "pass" and row["retry_count"] < max_retries:
            if not adapter.can_make_new_call():
                row["retry_budget_exhausted"] = True
                break
            retry_number = row["retry_count"] + 1
            retry_text, _ = adapter.call(prompt_for_retry(problem.prompt, current_candidate, final_judgement["output"], retry_number))
            current_candidate = extract_top_module(retry_text)
            final_judgement = as_dict(judge(problem, current_candidate))
            row["retry_count"] = retry_number
            row["attempts"].append({"attempt": retry_number, "candidate": current_candidate, "judge": final_judgement})
        row["looped_status"] = final_judgement["status"]
        row["fixed_by_retry"] = row["one_shot_status"] != "pass" and row["looped_status"] == "pass"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SiliconLoop's bounded agentic Verilog experiment.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/verilog_eval"))
    parser.add_argument("--output", type=Path, default=Path("site/data/results.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/claude_cache"))
    parser.add_argument("--problems", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--model", default="claude-haiku-5-5")
    args = parser.parse_args()

    problems = load_problems(args.data_dir)
    if not problems:
        raise SystemExit(f"No complete VerilogEval problems found in {args.data_dir}")
    selected, selection, excluded = select_hardest(problems, args.problems)
    if len(selected) < args.problems:
        raise SystemExit(f"Only {len(selected)} compatible problems found, requested {args.problems}")
    problem_by_id = {problem.problem_id: problem for problem in selected}
    adapter = ClaudeAdapter(args.model, args.cache_dir, max_new_calls=130)

    started = time.perf_counter()
    rows = []
    print(f"selection={','.join(problem.problem_id for problem in selected)}", flush=True)
    for problem in selected:
        print(f"one_shot problem={problem.problem_id}", flush=True)
        rows.append(initial_row(adapter, problem))
    apply_feedback_loop(adapter, problem_by_id, rows, args.retries)
    elapsed_seconds = round(time.perf_counter() - started, 3)

    one_shot = [{"status": row["one_shot_status"]} for row in rows]
    looped = [{"status": row["looped_status"]} for row in rows]
    one_shot_breakdown = Counter(row["one_shot_status"] for row in rows)
    looped_breakdown = Counter(row["looped_status"] for row in rows)
    replay_row = next((row for row in rows if row["fixed_by_retry"]), None)
    if replay_row is None:
        replay_row = next((row for row in rows if row["one_shot_status"] != "pass"), rows[0])
    replay = dict(replay_row)
    replay["prompt"] = problem_by_id[replay_row["problem_id"]].prompt
    replay["replay_note"] = (
        "Natural run evidence: the first candidate failed and the next candidate passed after simulator feedback."
        if replay_row["fixed_by_retry"]
        else "This run had no natural one-shot failure fixed by feedback. The displayed attempts are real failures from the selected run."
    )
    output = {
        "project": "SiliconLoop",
        "recorded_with": f"Claude Haiku 5.5 via Claude Code, {datetime.now().date().isoformat()}",
        "model": args.model,
        "dataset": "NVlabs VerilogEval dataset_spec-to-rtl",
        "dataset_target_problems": 156,
        "compatible_problem_count": len(problems) - len(excluded),
        "excluded_compatibility_problems": excluded,
        "selection_rule": SELECTION_RULE,
        "selected_problem_count": len(selected),
        "selection": selection,
        "max_retries": args.retries,
        "one_shot_pass_rate": pass_rate(one_shot),
        "looped_pass_rate": pass_rate(looped),
        "one_shot_passes": sum(row["status"] == "pass" for row in one_shot),
        "looped_passes": sum(row["status"] == "pass" for row in looped),
        "evaluated_attempts": len(rows),
        "fixed_by_retries": sum(row["fixed_by_retry"] for row in rows),
        "one_shot_failure_breakdown": dict(sorted(one_shot_breakdown.items())),
        "looped_failure_breakdown": dict(sorted(looped_breakdown.items())),
        "elapsed_seconds": elapsed_seconds,
        "mean_seconds_per_problem": round(sum(row["elapsed_seconds"] for row in rows) / len(rows), 3),
        "direct_api_cost_usd": 0,
        "llm_calls_used": adapter.actual_calls + adapter.cached_calls,
        "llm_calls_executed": adapter.actual_calls,
        "llm_cache_hits": adapter.cached_calls,
        "new_call_budget": adapter.max_new_calls,
        "retry_budget_exhausted": any(row.get("retry_budget_exhausted") for row in rows),
        "rows": rows,
        "replay": replay,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({key: output[key] for key in ("one_shot_pass_rate", "looped_pass_rate", "fixed_by_retries", "llm_calls_used", "llm_calls_executed", "elapsed_seconds")}, indent=2))


if __name__ == "__main__":
    main()
