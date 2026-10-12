from pathlib import Path

from siliconloop import Problem, extract_top_module, judge, load_problems, patch_icarus13_testbench, pass_rate


ROOT = Path(__file__).resolve().parents[1]
COMPAT = ROOT / "data" / "compatibility"


def test_extracts_module_from_markdown():
    text = "```verilog\nmodule TopModule(output zero); assign zero = 1'b0; endmodule\n```"
    assert extract_top_module(text).startswith("module TopModule")
    assert extract_top_module(text).endswith("endmodule")


def test_patches_only_forward_dump_reference():
    source = "initial $dumpvars(1, stim1.clk, tb_mismatch, zero_ref);"
    assert "tb_mismatch" not in patch_icarus13_testbench(source)


def test_icarus13_judge_passes_official_prob001_fixture():
    problem = Problem(
        "Prob001",
        "zero",
        COMPAT / "Prob001_zero_prompt.txt",
        COMPAT / "Prob001_zero_ref.sv",
        COMPAT / "Prob001_zero_test.sv",
    )
    result = judge(problem, (COMPAT / "Prob001_zero.sv").read_text())
    assert result.status == "pass"
    assert "Mismatches: 0" in result.output


def test_judge_classifies_wrong_output():
    problem = Problem(
        "Prob001",
        "zero",
        COMPAT / "Prob001_zero_prompt.txt",
        COMPAT / "Prob001_zero_ref.sv",
        COMPAT / "Prob001_zero_test.sv",
    )
    result = judge(problem, "module TopModule(output zero); assign zero = 1'b1; endmodule")
    assert result.status == "wrong_output"
    assert "Mismatches:" in result.output


def test_load_problems_and_rate():
    problems = load_problems(COMPAT)
    assert [problem.problem_id for problem in problems] == ["Prob001"]
    assert pass_rate([{"status": "pass"}, {"status": "wrong_output"}]) == 0.5
