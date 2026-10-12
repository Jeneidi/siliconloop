"""Black-box Verilog judge used by the SiliconLoop experiment."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Problem:
    problem_id: str
    title: str
    prompt_path: Path
    reference_path: Path
    test_path: Path

    @property
    def prompt(self) -> str:
        return self.prompt_path.read_text()


@dataclass(frozen=True)
class JudgeResult:
    status: str
    output: str


def load_problems(data_dir: Path) -> list[Problem]:
    problems = []
    for prompt_path in sorted(data_dir.glob("*_prompt.txt")):
        stem = prompt_path.name.removesuffix("_prompt.txt")
        reference_path = data_dir / f"{stem}_ref.sv"
        test_path = data_dir / f"{stem}_test.sv"
        if reference_path.exists() and test_path.exists():
            problem_id = stem.split("_", 1)[0]
            title = stem.split("_", 1)[1].replace("_", " ")
            problems.append(Problem(problem_id, title, prompt_path, reference_path, test_path))
    return problems


def patch_icarus13_testbench(testbench: str) -> str:
    """Remove the v13-only forward-reference failure from the debug dump list."""

    return re.sub(r",\s*tb_mismatch\s*,", ",", testbench, count=1)


def extract_top_module(model_text: str) -> str:
    fenced = re.search(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", model_text, re.IGNORECASE | re.DOTALL)
    source = fenced.group(1).strip() if fenced else model_text.strip()
    module = re.search(r"\bmodule\s+TopModule\b.*?\bendmodule\b", source, re.IGNORECASE | re.DOTALL)
    return module.group(0).strip() if module else source


def judge(problem: Problem, candidate: str, iverilog: str = "/opt/homebrew/bin/iverilog", timeout_seconds: int = 10) -> JudgeResult:
    with tempfile.TemporaryDirectory(prefix="siliconloop-") as scratch:
        scratch_path = Path(scratch)
        candidate_path = scratch_path / "candidate.sv"
        reference_path = scratch_path / "reference.sv"
        test_path = scratch_path / "test.sv"
        output_path = scratch_path / "simulation.out"
        candidate_path.write_text(candidate)
        reference_path.write_text(problem.reference_path.read_text())
        test_path.write_text(patch_icarus13_testbench(problem.test_path.read_text()))

        compile_command = [iverilog, "-g2012", "-o", str(output_path), str(candidate_path), str(reference_path), str(test_path)]
        try:
            compiled = subprocess.run(compile_command, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return JudgeResult("timeout", f"Icarus compile timed out after {timeout_seconds}s.\n{exc}")
        compile_output = (compiled.stdout + compiled.stderr).strip()
        if compiled.returncode != 0:
            return JudgeResult("compile_error", compile_output)

        try:
            simulated = subprocess.run(["/opt/homebrew/bin/vvp", str(output_path)], capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return JudgeResult("timeout", f"Simulation timed out after {timeout_seconds}s.\n{exc}")
        simulation_output = (simulated.stdout + simulated.stderr).strip()
        if "Mismatches: 0" in simulation_output:
            return JudgeResult("pass", simulation_output)
        if "TIMEOUT" in simulation_output:
            return JudgeResult("timeout", simulation_output)
        return JudgeResult("wrong_output", simulation_output)


def as_dict(result: JudgeResult) -> dict[str, str]:
    return asdict(result)


def pass_rate(results: list[dict]) -> float:
    if not results:
        return 0.0
    return sum(row["status"] == "pass" for row in results) / len(results)
