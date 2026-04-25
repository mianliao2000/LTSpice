# LTspice Visualization

Convert SPICE netlists into topology-aware LTspice `.asc` schematics, with optional PNG previews, visual scoring reports, and AI vision review.

The converter keeps the original netlist as the electrical source of truth. Recognized power-converter blocks are placed into readable canonical layouts, while unrecognized elements are still emitted in an auxiliary area so the generated schematic remains runnable.

## Features

- Parses SPICE `.cir` and `.net` files.
- Generates LTspice `.asc` schematics.
- Recognizes common power-converter topologies:
  - synchronous buck
  - asynchronous buck
  - boost
  - inverting buck-boost
- Can render PNG previews when Pillow is installed.
- Can run a built-in visual layout scorer and write JSON reports.
- Optional AI vision review through OpenAI or MiniMax.

## Files

- `netlist_to_canonical_asc.py` - main converter CLI and library code.
- `test_netlist_to_canonical_asc.py` - unit tests.
- `synchronous_buck_tran.cir`, `synchronous_buck_bode.cir`, `synchronous_buck_bode_ac.cir` - sample input netlists.
- `synchronous_buck_canonical.asc` - generated sample schematic.
- `synchronous_buck_canonical.png` - generated sample preview.
- `synchronous_buck_visual_report.json` - generated sample visual report.
- `nmos_sw.asy` - LTspice symbol used by the sample schematic.

## Requirements

- Python 3.10 or newer.
- Optional: `Pillow` for PNG preview rendering.
- Optional: `openai` for OpenAI vision review.
- Optional: MiniMax `mmx` CLI for MiniMax vision review.

Install optional Python dependencies as needed:

```powershell
python -m pip install Pillow openai
```

## Configuration

Copy `.env.example` to `.env` and fill in the values you need:

```powershell
Copy-Item .env.example .env
```

`.env` is loaded automatically by `netlist_to_canonical_asc.py`. Existing environment variables are not overwritten.

Required only when using the related provider:

- `OPENAI_API_KEY` - OpenAI API key for `--vision-provider openai`.
- `MINIMAX_API_KEY` - MiniMax API key for `--vision-provider minimax`.
- `MINIMAX_API_HOST` - MiniMax API host. Defaults to `https://api.minimax.io`.
- `VISION_PROVIDER` - default vision provider, either `openai` or `minimax`.
- `VISION_REVIEW_MODEL` - default vision model.

## Usage

Generate an LTspice schematic:

```powershell
python .\netlist_to_canonical_asc.py .\synchronous_buck_tran.cir -o .\synchronous_buck_canonical.asc
```

Generate a schematic and PNG preview:

```powershell
python .\netlist_to_canonical_asc.py .\synchronous_buck_tran.cir -o .\synchronous_buck_canonical.asc --preview .\synchronous_buck_canonical.png
```

Run the visual layout agent and write a report:

```powershell
python .\netlist_to_canonical_asc.py .\synchronous_buck_tran.cir -o .\synchronous_buck_canonical.asc --visual-agent --preview .\synchronous_buck_canonical.png --visual-report .\synchronous_buck_visual_report.json
```

Dump the recognized intermediate representation:

```powershell
python .\netlist_to_canonical_asc.py .\synchronous_buck_tran.cir --dump-ir
```

Force a topology when annotations are absent or ambiguous:

```powershell
python .\netlist_to_canonical_asc.py .\input.cir --topology synchronous_buck
```

Run OpenAI vision review:

```powershell
python .\netlist_to_canonical_asc.py .\synchronous_buck_tran.cir --preview .\review.png --vision-review --vision-provider openai
```

Run MiniMax vision review:

```powershell
python .\netlist_to_canonical_asc.py .\synchronous_buck_tran.cir --preview .\review.png --vision-review --vision-provider minimax
```

## Netlist Annotations

The converter can infer topology from common component names and connections. You can also provide visualization hints in comments using the parser-supported annotation format already exercised by the tests. Use `--dump-ir` to inspect what was recognized before generating a final schematic.

## Tests

Run the unit test suite:

```powershell
python -m unittest .\test_netlist_to_canonical_asc.py
```

## Security

Do not commit `.env` or real API keys. Use `.env.example` as the shareable template.
