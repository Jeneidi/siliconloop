# SiliconLoop

An agent writes Verilog chip-design code, then Icarus Verilog decides whether the circuit passes its testbench.

Live demo: https://siliconloop.mjeneidi.com

## The problem

LLM-generated RTL can look correct while producing the wrong signal in simulation. SiliconLoop gives Claude Haiku 5.5 an English VerilogEval specification, hides the reference solution and testbench, and uses the simulator as the black-box judge. This is the same verification habit that matters for hardware, edge devices, neural networks, PyTorch signal processing, wafer yield analytics, SQL workflows, and agentic AI systems.

## The result

On a 15-problem pilot repeated 3 times, one-shot generation passed 45 of 45 judged attempts, for a 100% pass rate. The retry loop also passed 45 of 45, for a 100% pass rate, so retries added no lift on this pilot. The uncached run used 45 Claude calls, averaged 6.108 seconds per problem, and had $0 direct API cost.

The official dataset has 156 problems. An Icarus Verilog 13.0 compatibility sweep passed 155. Prob099 was excluded because its official testbench references Y2 and Y4 ports that are missing from its reference module.

## How it works

1. Download the NVlabs VerilogEval spec-to-RTL prompts, references, and testbenches.
2. Ask Claude Haiku 5.5 for one standalone `TopModule` from the English spec.
3. Compile and simulate the candidate with Icarus Verilog 13.0 in a temporary folder.
4. Feed compiler or mismatch text back for up to 2 retries when the judge does not pass.
5. Compare one-shot and looped pass rates and publish the recorded results in the static site.

## Data and license

The data comes from the [NVlabs VerilogEval repository](https://github.com/NVlabs/verilog-eval), specifically `dataset_spec-to-rtl`. The source repository is MIT licensed. The downloader records the source and timestamp in `data/verilog_eval/manifest.json`.

## Evaluation method

The judge compiles the candidate, reference module, and official testbench with `iverilog -g2012`, then runs `vvp`. A run passes only when the testbench prints `Mismatches: 0`. Compile errors, wrong output, and timeouts are recorded separately. Each simulation has a 10-second timeout. The website replays the real Prob001 prompt and judge output without requiring an API key.

## How to run

Create the environment and install the pinned test dependency:

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python -r requirements.txt
```

Run the exact test gate:

```bash
.venv/bin/python -m pytest -q
```

Download the official dataset and run the bounded experiment:

```bash
.venv/bin/python scripts/download_dataset.py --output data/verilog_eval
.venv/bin/python scripts/run_experiment.py \
  --data-dir data/verilog_eval \
  --output site/data/results.json \
  --cache-dir data/claude_cache \
  --problems 15 \
  --repeats 3 \
  --retries 2 \
  --model claude-haiku-5-5
```

The experiment calls the Claude Code CLI on the user's subscription and caches every response. It does not call `api.openai.com`, read API keys, or use a paid API.

Serve the site locally:

```bash
cd site
python3 -m http.server 8000 --bind 127.0.0.1
```

## Limitations

This build reports a bounded 15-problem pilot rather than full-dataset model performance. The simplified build brief drops confidence intervals and statistical tests, so the site does not claim a 95% interval. All natural pilot generations passed on the first attempt, so the replay uses a clearly labeled controlled one-line failure fixture followed by the recorded candidate. Prob099 remains excluded from evaluation because its official fixture is incompatible with the installed Icarus 13.0 environment for the reason above. The static site is recorded evidence only and does not compile visitor-supplied RTL in the browser.

## Credits

Built by Mohammad Jeneidi for AI and ML internship applications. Python provides the orchestration, Icarus Verilog provides the executable verification, and the Project Site Kit provides the static presentation layer. The model outputs are recorded with Claude Haiku 5.5 via Claude Code on 2026-10-12.
