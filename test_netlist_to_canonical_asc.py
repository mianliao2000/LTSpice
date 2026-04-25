import tempfile
import unittest
from unittest.mock import patch
import os
from pathlib import Path

from netlist_to_canonical_asc import (
    CanonicalAscGenerator,
    MiniMaxVisionReviewer,
    OpenAIVisionReviewer,
    TopologyRecognizer,
    VisualQAAgent,
    VisualScorer,
    build_ir,
    load_env_file,
    pin_points,
    parse_asc,
    parse_netlist,
)


ROOT = Path(__file__).resolve().parent
BUCK_NETLIST = ROOT / "synchronous_buck_tran.cir"


class NetlistParserTests(unittest.TestCase):
    def test_load_env_file_sets_missing_values_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "MINIMAX_API_KEY=file-key\n"
                "MINIMAX_API_HOST=https://api.minimax.io\n"
                "QUOTED='quoted value'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MINIMAX_API_KEY": "existing-key"}, clear=False):
                loaded = load_env_file(env_path)

                self.assertEqual(loaded["MINIMAX_API_KEY"], "file-key")
                self.assertEqual(os.environ["MINIMAX_API_KEY"], "existing-key")
                self.assertEqual(os.environ["MINIMAX_API_HOST"], "https://api.minimax.io")
                self.assertEqual(os.environ["QUOTED"], "quoted value")

    def test_parser_keeps_components_directives_and_long_expressions(self):
        netlist = parse_netlist(BUCK_NETLIST)

        self.assertEqual(len(netlist.components), 25)
        self.assertEqual(len(netlist.params), 5)
        self.assertEqual(len(netlist.models), 3)
        self.assertIn(".tran 0 {tstop} 0 100n uic", netlist.directives)

        bctrl = netlist.by_name("Bctrl")
        self.assertIsNotNone(bctrl)
        self.assertEqual(bctrl.nodes, ("ctrl", "0"))
        self.assertIn("limit(Kp*V(err)+V(int)", bctrl.value)
        self.assertIn("0.98", bctrl.value)

        inductor = netlist.by_name("L1")
        self.assertEqual(inductor.value, "{L}")
        self.assertEqual(inductor.attrs, "ic={il_ic}")


class RecognitionTests(unittest.TestCase):
    def test_current_buck_netlist_recognizes_semantic_blocks(self):
        ir = build_ir(BUCK_NETLIST)

        self.assertEqual(ir.topology, "synchronous_buck")
        self.assertEqual(ir.power.nodes["vin"], "vin")
        self.assertEqual(ir.power.nodes["sw"], "sw")
        self.assertEqual(ir.power.nodes["vout"], "vout")
        self.assertEqual(ir.power.high_switch, "S_hi")
        self.assertEqual(ir.power.low_switch, "S_lo")
        self.assertEqual(ir.power.inductor, "L1")
        self.assertEqual(ir.power.output_cap, "Cout")
        self.assertEqual(ir.power.load_switch, "Sload")
        self.assertEqual(ir.control.duty, "Bctrl")
        self.assertEqual(ir.control.gate_hi, "Bgatehi")
        self.assertEqual(ir.control.gate_lo, "Bgatelo")

    def test_viz_annotation_can_override_topology_and_nodes(self):
        annotated = """* @viz topology=synchronous_buck
* @viz node vin=input sw=phase vout=out gnd=0
V1 input 0 12
S1 input phase gh 0 swmod
S2 phase 0 gl 0 swmod
L1 phase out 10u
C1 out 0 22u
R1 out 0 5
.model swmod SW(Ron=1m Roff=1G Vt=2 Vh=.1)
.tran 1m
.end
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "annotated.cir"
            path.write_text(annotated, encoding="utf-8")
            netlist = parse_netlist(path)
            ir = TopologyRecognizer(netlist).recognize()

        self.assertEqual(ir.topology, "synchronous_buck")
        self.assertEqual(ir.power.nodes["vin"], "input")
        self.assertEqual(ir.power.nodes["sw"], "phase")
        self.assertEqual(ir.power.nodes["vout"], "out")


class GeneratorTests(unittest.TestCase):
    def test_generator_emits_runnable_canonical_asc_surface(self):
        ir = build_ir(BUCK_NETLIST)
        asc = CanonicalAscGenerator(ir).generate()

        self.assertTrue(asc.startswith("Version 4\nSHEET 1 "))
        self.assertIn("Canonical synchronous buck schematic", asc)
        self.assertIn("Half bridge", asc)
        self.assertIn("Output inductor", asc)
        self.assertRegex(asc, r"FLAG \d+ \d+ lx")
        self.assertIn("SYMBOL nmos_sw", asc)
        self.assertIn("SYMATTR InstName X_S_hi", asc)
        self.assertIn("SYMATTR InstName X_S_lo", asc)
        self.assertIn("!.subckt nmos_sw_body D S G", asc)
        self.assertIn("@symmetry L1 R_L Cout Rfixed Sload", asc)
        self.assertIn("SYMATTR InstName Bctrl", asc)
        self.assertIn("!.param Vdc=12", asc)
        self.assertIn("!.tran 0 {tstop} 0 100n uic", asc)
        self.assertNotIn(".include synchronous_buck_tran.cir", asc)

        inst_names = [
            line.replace("SYMATTR InstName ", "")
            for line in asc.splitlines()
            if line.startswith("SYMATTR InstName ")
        ]
        self.assertEqual(len(inst_names), len(set(inst_names)))
        expected_names = {component.name for component in ir.netlist.components}
        expected_names.remove("S_hi")
        expected_names.remove("S_lo")
        expected_names.remove("D_hi")
        expected_names.remove("D_lo")
        expected_names.update({"X_S_hi", "X_S_lo"})
        self.assertEqual(set(inst_names), expected_names)

    def test_buck_vertical_branches_are_centered_on_their_wire_segments(self):
        ir = build_ir(BUCK_NETLIST)
        asc = CanonicalAscGenerator(ir).generate()
        parsed = parse_asc(asc)

        symbols = {symbol.inst_name: symbol for symbol in parsed.symbols}
        flags = {name: (x, y) for x, y, name in parsed.flags}
        phase_y = flags["vout"][1] + 64
        ground_y = next(y for x, y, name in parsed.flags if name == "0" and x == 160)
        nload_y = flags["nload"][1]
        vin_y = flags["vin"][1]

        def pin_midpoint_y(inst_name: str, symbol_name: str) -> float:
            symbol = symbols[inst_name]
            pins = pin_points(symbol_name, symbol.x, symbol.y, symbol.rotation, 2)
            return (pins[0][1] + pins[1][1]) / 2

        self.assertLessEqual(abs(pin_midpoint_y("Vdc", "voltage") - ((vin_y + ground_y) / 2)), 16)
        self.assertLessEqual(abs(pin_midpoint_y("Rfixed", "res") - ((phase_y + ground_y) / 2)), 16)
        self.assertLessEqual(abs(pin_midpoint_y("Rstep", "res") - ((nload_y + ground_y) / 2)), 16)

        high = symbols["X_S_hi"]
        low = symbols["X_S_lo"]
        high_pins = pin_points("nmos_sw", high.x, high.y, high.rotation, 2)
        low_pins = pin_points("nmos_sw", low.x, low.y, low.rotation, 2)
        self.assertLess(high_pins[1][1], phase_y)
        self.assertGreater(low_pins[0][1], phase_y)
        self.assertGreaterEqual(low_pins[0][1] - high_pins[1][1], 96)
        self.assertLessEqual(abs(((high_pins[0][1] + high_pins[1][1]) / 2) - ((vin_y + phase_y) / 2)), 16)
        self.assertLessEqual(abs(((low_pins[0][1] + low_pins[1][1]) / 2) - ((phase_y + ground_y) / 2)), 16)

        sload = symbols["Sload"]
        sload_power_pins = pin_points("sw", sload.x, sload.y, sload.rotation, 2)
        rstep = symbols["Rstep"]
        rstep_pins = pin_points("res", rstep.x, rstep.y, rstep.rotation, 2)
        self.assertEqual(sload_power_pins[0][0], rstep_pins[0][0])
        self.assertTrue(any(y1 == y2 == nload_y and x1 != x2 for x1, y1, x2, y2 in parsed.wires))
        self.assertLess(flags["lx"][1], phase_y)
        self.assertLess(flags["vout"][1], phase_y)
        self.assertGreater(flags["nload"][0], rstep_pins[0][0])
        self.assertLess(flags["load_ctl"][0], sload.x)

        res_pins = pin_points("res", symbols["R_ESR"].x, symbols["R_ESR"].y, symbols["R_ESR"].rotation, 2)
        cap_pins = pin_points("cap", symbols["Cout"].x, symbols["Cout"].y, symbols["Cout"].rotation, 2)
        gaps = [
            min(res_pins[0][1], res_pins[1][1]) - phase_y,
            min(cap_pins[0][1], cap_pins[1][1]) - max(res_pins[0][1], res_pins[1][1]),
            ground_y - max(cap_pins[0][1], cap_pins[1][1]),
        ]
        self.assertLessEqual(max(gaps) - min(gaps), 32)


class VisualQaTests(unittest.TestCase):
    def test_asc_parser_and_scorer_detect_overlap(self):
        asc = """Version 4
SHEET 1 400 300
TEXT 16 48 Left 2 ; one
TEXT 18 50 Left 2 ; two
SYMBOL res 16 32 R0
WINDOW 0 44 16 Invisible 2
WINDOW 3 44 72 Invisible 2
SYMATTR InstName R1
SYMATTR Value 1k
"""
        parsed = parse_asc(asc)
        score = VisualScorer().score(parsed)

        self.assertEqual(parsed.sheet_width, 400)
        self.assertEqual(len(parsed.texts), 2)
        self.assertEqual(len(parsed.symbols), 1)
        self.assertGreater(score.collisions, 0)

    def test_visual_agent_selects_collision_free_current_buck(self):
        ir = build_ir(BUCK_NETLIST)
        result = VisualQAAgent(ir, max_iterations=20).run()

        self.assertEqual(result.score.score, 0)
        self.assertEqual(result.score.collisions, 0)
        self.assertEqual(result.score.out_of_bounds, 0)
        self.assertIn("selected_profile", result.report)
        self.assertIn("Rswleak", result.asc_text)
        self.assertIn("Bloadctl", result.asc_text)
        self.assertIn("@symmetry L1 R_L Cout Rfixed Sload", result.asc_text)

    def test_vision_reviewer_parses_json_response(self):
        reviewer = OpenAIVisionReviewer(model="test-model", api_key="test-key")

        class FakeResponse:
            output_text = '{"readability_score": 8, "topology_similarity_score": 7, "verdict": "pass", "strengths": [], "problems": [], "layout_actions": []}'

        class FakeResponses:
            @staticmethod
            def create(**kwargs):
                self = kwargs
                self  # keep lint-free while preserving the request for debugging if needed
                return FakeResponse()

        class FakeClient:
            def __init__(self, api_key):
                self.api_key = api_key
                self.responses = FakeResponses()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "preview.png"
            image_path.write_bytes(b"fake-png-bytes")
            with patch("openai.OpenAI", FakeClient):
                result = reviewer.review(
                    preview_path=image_path,
                    topology="synchronous_buck",
                    visual_score=VisualScorer().score(parse_asc("Version 4\nSHEET 1 100 100\n")),
                )

        self.assertEqual(result.model, "test-model")
        self.assertEqual(result.review["verdict"], "pass")
        self.assertEqual(result.review["readability_score"], 8)

    def test_minimax_vision_reviewer_uses_mmx_cli(self):
        reviewer = MiniMaxVisionReviewer(
            model="mmx-vision",
            api_key="test-key",
            api_host="https://example.test",
            command="mmx-test",
        )

        captured = {}

        class FakeCompleted:
            returncode = 0
            stderr = ""
            stdout = '{"content":"<think>private notes</think>{\\"readability_score\\": 6, \\"topology_similarity_score\\": 5, \\"verdict\\": \\"needs_revision\\", \\"strengths\\": [], \\"problems\\": [\\"labels too small\\"], \\"layout_actions\\": []}"}'

        def fake_run(command, text, capture_output, timeout):
            captured["command"] = command
            captured["text"] = text
            captured["capture_output"] = capture_output
            captured["timeout"] = timeout
            return FakeCompleted()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "preview.png"
            image_path.write_bytes(b"fake-png-bytes")
            with patch("subprocess.run", fake_run):
                result = reviewer.review(
                    preview_path=image_path,
                    topology="synchronous_buck",
                    visual_score=VisualScorer().score(parse_asc("Version 4\nSHEET 1 100 100\n")),
                )

        command = captured["command"]
        self.assertTrue(command[0].endswith("mmx-test") or command[0].endswith("mmx-test.cmd"))
        self.assertEqual(command[1:3], ["vision", "describe"])
        self.assertIn("--image", command)
        self.assertIn("--prompt", command)
        self.assertIn("--output", command)
        self.assertIn("json", command)
        self.assertIn("--base-url", command)
        self.assertIn("https://example.test", command)
        self.assertIn("--api-key", command)
        self.assertIn("test-key", command)
        self.assertEqual(result.review["verdict"], "needs_revision")
        self.assertEqual(result.review["readability_score"], 6)

    def test_minimax_vision_reviewer_normalizes_fractional_scores(self):
        reviewer = MiniMaxVisionReviewer(
            model="mmx-vision",
            api_key="test-key",
            api_host="https://example.test",
            command="mmx-test",
        )

        class FakeCompleted:
            returncode = 0
            stderr = ""
            stdout = '{"content":"```json\\n{\\"readability_score\\": 0.8, \\"topology_similarity_score\\": 1.0, \\"verdict\\": \\"pass\\"}\\n```"}'

        def fake_run(command, text, capture_output, timeout):
            return FakeCompleted()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "preview.png"
            image_path.write_bytes(b"fake-png-bytes")
            with patch("subprocess.run", fake_run):
                result = reviewer.review(
                    preview_path=image_path,
                    topology="synchronous_buck",
                    visual_score=VisualScorer().score(parse_asc("Version 4\nSHEET 1 100 100\n")),
                )

        self.assertEqual(result.review["verdict"], "pass")
        self.assertEqual(result.review["readability_score"], 8.0)
        self.assertEqual(result.review["topology_similarity_score"], 10.0)
        self.assertEqual(result.review["strengths"], [])


if __name__ == "__main__":
    unittest.main()
