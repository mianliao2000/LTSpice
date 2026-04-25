#!/usr/bin/env python3
"""Convert a SPICE netlist into a topology-aware, readable LTspice ASC file.

This is intentionally conservative: the netlist remains the source of the
electrical truth, while the generated schematic uses canonical power-converter
placement for the parts it can understand. Any parsed element that is not part
of the canonical template is still emitted in a flagged auxiliary area so the
generated ASC remains runnable instead of becoming a picture-only artifact.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


GROUND_NAMES = {"0", "gnd", "GND"}


def load_env_file(path: Path = Path(".env")) -> dict[str, str]:
    """Load simple KEY=VALUE pairs into os.environ without overwriting existing values."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


@dataclass(frozen=True)
class Component:
    name: str
    kind: str
    nodes: tuple[str, ...]
    value: str = ""
    attrs: str = ""
    raw: str = ""
    line_no: int = 0

    @property
    def key(self) -> str:
        return self.name.lower()

    def other_node(self, node: str) -> Optional[str]:
        power_nodes = self.nodes[:2]
        if len(power_nodes) != 2 or node not in power_nodes:
            return None
        return power_nodes[1] if power_nodes[0] == node else power_nodes[0]

    def connects(self, *nodes: str) -> bool:
        wanted = set(nodes)
        return wanted.issubset(set(self.nodes))


@dataclass
class VizAnnotations:
    topology: Optional[str] = None
    nodes: dict[str, str] = field(default_factory=dict)
    control: dict[str, str] = field(default_factory=dict)
    raw: list[str] = field(default_factory=list)


@dataclass
class Netlist:
    path: Optional[Path]
    title: str = ""
    comments: list[str] = field(default_factory=list)
    annotations: VizAnnotations = field(default_factory=VizAnnotations)
    components: list[Component] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def by_name(self, name: str) -> Optional[Component]:
        name = name.lower()
        for comp in self.components:
            if comp.key == name:
                return comp
        return None

    def components_of(self, kind: str) -> list[Component]:
        kind = kind.upper()
        return [comp for comp in self.components if comp.kind == kind]

    def graph(self) -> dict[str, list[Component]]:
        graph: dict[str, list[Component]] = {}
        for comp in self.components:
            for node in comp.nodes:
                graph.setdefault(node, []).append(comp)
        return graph


@dataclass
class PowerStage:
    topology: str
    nodes: dict[str, str]
    input_source: Optional[str] = None
    high_switch: Optional[str] = None
    low_switch: Optional[str] = None
    main_switch: Optional[str] = None
    sync_switch: Optional[str] = None
    freewheel_diode: Optional[str] = None
    high_body_diode: Optional[str] = None
    low_body_diode: Optional[str] = None
    inductor: Optional[str] = None
    inductor_esr: Optional[str] = None
    output_cap: Optional[str] = None
    cap_esr: Optional[str] = None
    fixed_load: Optional[str] = None
    switch_leak: Optional[str] = None
    load_switch: Optional[str] = None
    step_load: Optional[str] = None
    load_control: Optional[str] = None


@dataclass
class ControlLoop:
    kind: str = "pid_pwm"
    ramp: Optional[str] = None
    error: Optional[str] = None
    integrator_source: Optional[str] = None
    integrator_cap: Optional[str] = None
    integrator_leak: Optional[str] = None
    derivative_source: Optional[str] = None
    derivative_cap: Optional[str] = None
    derivative_leak: Optional[str] = None
    duty: Optional[str] = None
    gate_hi: Optional[str] = None
    gate_lo: Optional[str] = None


@dataclass
class VisualIR:
    topology: str
    power: PowerStage
    control: ControlLoop
    netlist: Netlist


def grid(value: float, step: int = 16) -> int:
    return int(round(value / step) * step)


def even_points(start: int, end: int, count: int, step: int = 16) -> list[int]:
    """Return count grid-snapped points evenly spaced inside a line segment."""
    if count <= 0:
        return []
    span = end - start
    return [grid(start + span * (index + 1) / (count + 1), step=step) for index in range(count)]


def two_pin_origin_for_center(symbol: str, rotation: str, center: int) -> int:
    """Return origin y/x so a two-pin symbol's pin midpoint lands on center."""
    pin_offsets = pin_points(symbol, 0, 0, rotation, 2)
    if len(pin_offsets) < 2:
        return center
    if rotation in {"R270", "R90"}:
        midpoint = (pin_offsets[0][0] + pin_offsets[1][0]) / 2
    else:
        midpoint = (pin_offsets[0][1] + pin_offsets[1][1]) / 2
    return grid(center - midpoint)


def vertical_series_origins(
    top: int,
    bottom: int,
    specs: list[tuple[str, str]],
    step: int = 16,
) -> list[int]:
    """Place multiple vertical components with equal empty gaps between endpoints."""
    spans = []
    for symbol, rotation in specs:
        pins = pin_points(symbol, 0, 0, rotation, 2)
        spans.append(abs(pins[1][1] - pins[0][1]) if len(pins) >= 2 else 0)
    if not spans:
        return []
    empty = max(0, bottom - top - sum(spans))
    gap = empty / (len(spans) + 1)
    cursor = top + gap
    origins: list[int] = []
    for (symbol, rotation), span in zip(specs, spans):
        pins = pin_points(symbol, 0, 0, rotation, 2)
        first_pin = min(pins[0][1], pins[1][1]) if len(pins) >= 2 else 0
        origin = grid(cursor - first_pin, step=step)
        origins.append(origin)
        cursor += span + gap
    return origins


@dataclass(frozen=True)
class LayoutProfile:
    """Parameterized placement knobs for canonical schematic generation."""

    name: str = "readable"
    sheet_width: int = 2700
    sheet_height: int = 1850
    title_x: int = 80
    title_y: int = 32
    input_x: int = 160
    top_y: int = 240
    ground_y: int = 752
    bridge_x: int = 640
    inductor_x: int = 896
    dcr_x: int = 1056
    cap_x: int = 1280
    load_x: int = 1520
    sload_x: int = 1776
    rstep_x: int = 2016
    bus_end_x: int = 2240
    symmetry: bool = True
    control_y: int = 1008
    control_step_x: int = 288
    directive_y: int = 1440
    aux_y: int = 1280
    hide_symbol_windows: bool = True
    label_size: int = 2

    @classmethod
    def buck(cls, x_scale: float = 1.0, control_y: int = 1008, ground_y: int = 752) -> "LayoutProfile":
        start = 160

        def sx(offset: int) -> int:
            return grid(start + offset * x_scale)

        bus_end = sx(2080)
        return cls(
            name=f"buck_x{x_scale:.2f}_cy{control_y}_gy{ground_y}",
            sheet_width=max(2700, bus_end + 360),
            sheet_height=max(2050, control_y + 1040),
            input_x=start,
            top_y=240,
            ground_y=ground_y,
            bridge_x=sx(480),
            inductor_x=sx(736),
            dcr_x=sx(896),
            cap_x=sx(1168),
            load_x=sx(1424),
            sload_x=sx(1696),
            rstep_x=sx(1936),
            bus_end_x=bus_end,
            symmetry=True,
            control_y=control_y,
            control_step_x=max(272, grid(288 * x_scale)),
            directive_y=max(control_y + 384, 1424),
            aux_y=max(control_y + 240, 1248),
        )

    @classmethod
    def legacy(cls) -> "LayoutProfile":
        return cls(
            name="legacy",
            sheet_width=2100,
            sheet_height=1750,
            input_x=128,
            top_y=192,
            ground_y=640,
            bridge_x=512,
            inductor_x=640,
            dcr_x=760,
            cap_x=960,
            load_x=1104,
            sload_x=1264,
            rstep_x=1360,
            bus_end_x=1560,
            symmetry=False,
            control_y=832,
            control_step_x=280,
            directive_y=1264,
            aux_y=1104,
            hide_symbol_windows=False,
        )


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    kind: str
    ident: str
    owner: str = ""

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    @property
    def area(self) -> int:
        return self.width * self.height

    def expanded(self, padding: int) -> "Box":
        return Box(
            self.x1 - padding,
            self.y1 - padding,
            self.x2 + padding,
            self.y2 + padding,
            self.kind,
            self.ident,
            self.owner,
        )

    def intersects(self, other: "Box") -> bool:
        return self.x1 < other.x2 and self.x2 > other.x1 and self.y1 < other.y2 and self.y2 > other.y1

    def intersection_area(self, other: "Box") -> int:
        if not self.intersects(other):
            return 0
        return max(0, min(self.x2, other.x2) - max(self.x1, other.x1)) * max(
            0, min(self.y2, other.y2) - max(self.y1, other.y1)
        )


@dataclass
class AscText:
    x: int
    y: int
    size: int
    text: str
    source: str


@dataclass
class AscSymbol:
    name: str
    x: int
    y: int
    rotation: str
    inst_name: str = ""
    value: str = ""
    windows: dict[int, tuple[int, int, bool]] = field(default_factory=dict)


@dataclass
class ParsedAsc:
    sheet_width: int
    sheet_height: int
    texts: list[AscText] = field(default_factory=list)
    wires: list[tuple[int, int, int, int]] = field(default_factory=list)
    flags: list[tuple[int, int, str]] = field(default_factory=list)
    symbols: list[AscSymbol] = field(default_factory=list)
    symmetry_lines: list[list[str]] = field(default_factory=list)


@dataclass
class VisualIssue:
    kind: str
    severity: int
    message: str
    objects: list[str]
    box: tuple[int, int, int, int]


@dataclass
class VisualScore:
    score: int
    collisions: int
    out_of_bounds: int
    issues: list[VisualIssue]

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "collisions": self.collisions,
            "out_of_bounds": self.out_of_bounds,
            "issues": [asdict(issue) for issue in self.issues],
        }


def strip_inline_comment(line: str) -> str:
    """Remove plain semicolon comments while leaving behavioral expressions alone."""
    if ";" not in line:
        return line
    return line.split(";", 1)[0].rstrip()


def logical_lines(lines: Iterable[str]) -> Iterable[tuple[int, str]]:
    current = ""
    start_line = 0
    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.rstrip("\n\r")
        stripped = line.strip()
        if not stripped:
            if current:
                yield start_line, current
                current = ""
            continue
        if stripped.startswith("+"):
            current += " " + stripped[1:].strip()
            continue
        if current:
            yield start_line, current
        current = stripped
        start_line = line_no
    if current:
        yield start_line, current


def parse_key_values(tokens: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            values[key.strip().lower()] = value.strip()
    return values


def parse_viz_annotation(comment: str, annotations: VizAnnotations) -> None:
    body = comment.lstrip("*;").strip()
    if not body.startswith("@viz"):
        return
    payload = body[len("@viz") :].strip()
    annotations.raw.append(payload)
    if not payload:
        return

    tokens = payload.split()
    category = ""
    if tokens and "=" not in tokens[0]:
        category = tokens.pop(0).lower()

    kv = parse_key_values(tokens)
    if "topology" in kv:
        annotations.topology = kv["topology"]
    if category == "node":
        annotations.nodes.update(kv)
    elif category == "control":
        annotations.control.update(kv)


def parse_netlist(path: Path) -> Netlist:
    text = path.read_text(encoding="utf-8")
    netlist = Netlist(path=path)
    for line_no, line in logical_lines(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("*") or stripped.startswith(";"):
            netlist.comments.append(stripped)
            parse_viz_annotation(stripped, netlist.annotations)
            if not netlist.title and stripped.startswith("*") and not stripped.startswith("* @viz"):
                netlist.title = stripped.lstrip("*").strip()
            continue
        if stripped.startswith("."):
            directive = strip_inline_comment(stripped)
            if not directive:
                continue
            lower = directive.lower()
            if lower == ".end":
                continue
            netlist.directives.append(directive)
            if lower.startswith(".param"):
                netlist.params.append(directive)
            elif lower.startswith(".model"):
                netlist.models.append(directive)
            continue

        component = parse_component(stripped, line_no)
        if component:
            netlist.components.append(component)
    return netlist


def parse_component(line: str, line_no: int) -> Optional[Component]:
    clean = strip_inline_comment(line)
    if not clean:
        return None
    name = clean.split(None, 1)[0]
    kind = name[0].upper()

    node_counts = {
        "R": 2,
        "C": 2,
        "L": 2,
        "V": 2,
        "I": 2,
        "D": 2,
        "S": 4,
        "B": 2,
        "E": 2,
        "G": 2,
    }
    node_count = node_counts.get(kind)
    if node_count is None:
        return Component(name=name, kind=kind, nodes=(), value=clean, raw=line, line_no=line_no)

    pieces = clean.split(None, node_count + 1)
    if len(pieces) < node_count + 1:
        raise ValueError(f"Line {line_no}: cannot parse component: {line}")
    nodes = tuple(pieces[1 : 1 + node_count])
    rest = pieces[1 + node_count] if len(pieces) > 1 + node_count else ""

    value, attrs = split_value_attrs(kind, rest)
    return Component(
        name=name,
        kind=kind,
        nodes=nodes,
        value=value,
        attrs=attrs,
        raw=line,
        line_no=line_no,
    )


def split_value_attrs(kind: str, rest: str) -> tuple[str, str]:
    rest = rest.strip()
    if not rest:
        return "", ""
    if kind in {"B", "V", "I", "E", "G"}:
        return rest, ""
    parts = rest.split(None, 1)
    value = parts[0]
    attrs = parts[1] if len(parts) > 1 else ""
    return value, attrs


def find_between(components: Iterable[Component], kind: str, node_a: str, node_b: str) -> Optional[Component]:
    wanted = {node_a, node_b}
    for comp in components:
        if comp.kind == kind.upper() and set(comp.nodes[:2]) == wanted:
            return comp
    return None


def first_component_named(netlist: Netlist, *names: str) -> Optional[Component]:
    for name in names:
        comp = netlist.by_name(name)
        if comp:
            return comp
    return None


def find_b_source(netlist: Netlist, *patterns: str) -> Optional[Component]:
    lowered_patterns = [pattern.lower() for pattern in patterns]
    for comp in netlist.components_of("B"):
        haystack = f"{comp.name} {comp.value}".lower()
        if all(pattern in haystack for pattern in lowered_patterns):
            return comp
    return None


def node_is_ground(node: str) -> bool:
    return node in GROUND_NAMES or node.lower() in GROUND_NAMES


class TopologyRecognizer:
    def __init__(self, netlist: Netlist, topology_override: Optional[str] = None):
        self.netlist = netlist
        self.topology_override = topology_override

    def recognize(self) -> VisualIR:
        hinted_topology = self.topology_override or self.netlist.annotations.topology
        if hinted_topology:
            hinted_topology = hinted_topology.lower()

        nodes = dict(self.netlist.annotations.nodes)
        nodes.setdefault("gnd", "0")
        input_source = self._find_input_source(nodes)
        if input_source and "vin" not in nodes:
            nodes["vin"] = input_source.other_node(nodes["gnd"]) or input_source.nodes[0]

        topology = hinted_topology or ""
        if topology in {"synchronous_buck", "buck", "asynchronous_buck"}:
            power = self._recognize_buck(nodes, topology)
        elif topology == "boost":
            power = self._recognize_boost(nodes)
        elif topology in {"inverting_buck_boost", "buck_boost"}:
            power = self._recognize_inverting_buck_boost(nodes)
        else:
            power = self._auto_recognize(nodes)
            topology = power.topology

        control = self._recognize_control()
        return VisualIR(topology=power.topology, power=power, control=control, netlist=self.netlist)

    def _find_input_source(self, nodes: dict[str, str]) -> Optional[Component]:
        gnd = nodes.get("gnd", "0")
        for comp in self.netlist.components_of("V"):
            if gnd in comp.nodes or any(node_is_ground(node) for node in comp.nodes):
                return comp
        return self.netlist.components_of("V")[0] if self.netlist.components_of("V") else None

    def _auto_recognize(self, nodes: dict[str, str]) -> PowerStage:
        buck = self._recognize_buck(dict(nodes), "")
        if buck.inductor and (buck.high_switch or buck.main_switch):
            return buck

        boost = self._recognize_boost(dict(nodes))
        if boost.inductor and boost.main_switch and boost.freewheel_diode:
            return boost

        return self._recognize_inverting_buck_boost(dict(nodes))

    def _recognize_buck(self, nodes: dict[str, str], topology_hint: str) -> PowerStage:
        vin = nodes.get("vin")
        gnd = nodes.get("gnd", "0")
        switches = self.netlist.components_of("S")
        diodes = self.netlist.components_of("D")

        high_switch = None
        low_switch = None
        sw = nodes.get("sw")

        if vin:
            for high in switches:
                if vin not in high.nodes[:2]:
                    continue
                candidate_sw = high.other_node(vin)
                if not candidate_sw:
                    continue
                for low in switches:
                    if low == high:
                        continue
                    if candidate_sw in low.nodes[:2] and gnd in low.nodes[:2]:
                        high_switch = high
                        low_switch = low
                        sw = candidate_sw
                        break
                if high_switch:
                    break

        if not high_switch and vin:
            for switch in switches:
                if vin in switch.nodes[:2]:
                    high_switch = switch
                    sw = switch.other_node(vin)
                    break

        freewheel = None
        if sw:
            freewheel = find_between(diodes, "D", gnd, sw)

        if sw:
            nodes["sw"] = sw

        inductor = None
        inductor_esr = None
        vout = nodes.get("vout")
        lx = nodes.get("lx")
        if sw:
            for comp in self.netlist.components_of("L"):
                if sw in comp.nodes:
                    inductor = comp
                    lx = comp.other_node(sw)
                    nodes["lx"] = lx or nodes.get("lx", "")
                    break

        if inductor and lx:
            for comp in self.netlist.components_of("R"):
                if lx in comp.nodes and not comp.name.lower().startswith(("rint", "ref")):
                    maybe_vout = comp.other_node(lx)
                    if maybe_vout and not node_is_ground(maybe_vout):
                        inductor_esr = comp
                        vout = maybe_vout
                        break
            if not vout:
                vout = lx
        if vout:
            nodes["vout"] = vout

        cap, cap_esr = self._find_output_cap(vout, gnd)
        fixed_load = self._find_load(vout, gnd)
        load_switch, step_load, load_control = self._find_step_load(vout, gnd)

        high_body = find_between(diodes, "D", sw, vin) if sw and vin else None
        low_body = find_between(diodes, "D", gnd, sw) if sw else None
        switch_leak = find_between(self.netlist.components_of("R"), "R", sw, gnd) if sw else None

        if topology_hint == "asynchronous_buck" or (freewheel and not low_switch):
            topology = "asynchronous_buck"
        elif topology_hint == "buck":
            topology = "synchronous_buck" if low_switch else "asynchronous_buck"
        else:
            topology = topology_hint or ("synchronous_buck" if low_switch else "asynchronous_buck")

        return PowerStage(
            topology=topology,
            nodes=nodes,
            input_source=self._component_name(self._find_input_source(nodes)),
            high_switch=self._component_name(high_switch),
            low_switch=self._component_name(low_switch),
            main_switch=self._component_name(high_switch),
            sync_switch=self._component_name(low_switch),
            freewheel_diode=self._component_name(freewheel),
            high_body_diode=self._component_name(high_body),
            low_body_diode=self._component_name(low_body),
            inductor=self._component_name(inductor),
            inductor_esr=self._component_name(inductor_esr),
            output_cap=self._component_name(cap),
            cap_esr=self._component_name(cap_esr),
            fixed_load=self._component_name(fixed_load),
            switch_leak=self._component_name(switch_leak),
            load_switch=self._component_name(load_switch),
            step_load=self._component_name(step_load),
            load_control=self._component_name(load_control),
        )

    def _recognize_boost(self, nodes: dict[str, str]) -> PowerStage:
        vin = nodes.get("vin")
        gnd = nodes.get("gnd", "0")
        sw = nodes.get("sw")
        inductor = None
        main_switch = None
        diode = None
        vout = nodes.get("vout")

        if vin:
            for comp in self.netlist.components_of("L"):
                if vin in comp.nodes:
                    inductor = comp
                    sw = comp.other_node(vin)
                    break
        if sw:
            nodes["sw"] = sw
            main_switch = find_between(self.netlist.components_of("S"), "S", sw, gnd)
            for comp in self.netlist.components_of("D"):
                if sw in comp.nodes:
                    other = comp.other_node(sw)
                    if other and not node_is_ground(other):
                        diode = comp
                        vout = other
                        break
        if vout:
            nodes["vout"] = vout
        cap, cap_esr = self._find_output_cap(vout, gnd)
        fixed_load = self._find_load(vout, gnd)

        return PowerStage(
            topology="boost",
            nodes=nodes,
            input_source=self._component_name(self._find_input_source(nodes)),
            main_switch=self._component_name(main_switch),
            freewheel_diode=self._component_name(diode),
            inductor=self._component_name(inductor),
            output_cap=self._component_name(cap),
            cap_esr=self._component_name(cap_esr),
            fixed_load=self._component_name(fixed_load),
        )

    def _recognize_inverting_buck_boost(self, nodes: dict[str, str]) -> PowerStage:
        vin = nodes.get("vin")
        gnd = nodes.get("gnd", "0")
        sw = nodes.get("sw")
        inductor = None
        main_switch = None
        diode = None
        vout = nodes.get("vout")

        if vin:
            for comp in self.netlist.components_of("L"):
                if vin in comp.nodes:
                    inductor = comp
                    sw = comp.other_node(vin)
                    break
        if sw:
            nodes["sw"] = sw
            main_switch = find_between(self.netlist.components_of("S"), "S", sw, gnd)
            for comp in self.netlist.components_of("D"):
                if sw in comp.nodes:
                    other = comp.other_node(sw)
                    if other and not node_is_ground(other):
                        diode = comp
                        vout = other
                        break
        if vout:
            nodes["vout"] = vout
        cap, cap_esr = self._find_output_cap(vout, gnd)
        fixed_load = self._find_load(vout, gnd)

        return PowerStage(
            topology="inverting_buck_boost",
            nodes=nodes,
            input_source=self._component_name(self._find_input_source(nodes)),
            main_switch=self._component_name(main_switch),
            freewheel_diode=self._component_name(diode),
            inductor=self._component_name(inductor),
            output_cap=self._component_name(cap),
            cap_esr=self._component_name(cap_esr),
            fixed_load=self._component_name(fixed_load),
        )

    def _find_output_cap(
        self, vout: Optional[str], gnd: str
    ) -> tuple[Optional[Component], Optional[Component]]:
        if not vout:
            return None, None
        caps = self.netlist.components_of("C")
        resistors = self.netlist.components_of("R")

        direct = find_between(caps, "C", vout, gnd)
        if direct:
            return direct, None

        for resistor in resistors:
            if vout not in resistor.nodes:
                continue
            ncap = resistor.other_node(vout)
            if not ncap or node_is_ground(ncap):
                continue
            cap = find_between(caps, "C", ncap, gnd)
            if cap:
                return cap, resistor
        return None, None

    def _find_load(self, vout: Optional[str], gnd: str) -> Optional[Component]:
        if not vout:
            return None
        candidates = []
        for comp in self.netlist.components_of("R"):
            if set(comp.nodes[:2]) == {vout, gnd}:
                lowered = comp.name.lower()
                if lowered.startswith(("rint", "ref")):
                    continue
                candidates.append(comp)
        named = [comp for comp in candidates if "load" in comp.name.lower() or "fixed" in comp.name.lower()]
        return named[0] if named else (candidates[0] if candidates else None)

    def _find_step_load(
        self, vout: Optional[str], gnd: str
    ) -> tuple[Optional[Component], Optional[Component], Optional[Component]]:
        if not vout:
            return None, None, None
        for switch in self.netlist.components_of("S"):
            if vout not in switch.nodes[:2]:
                continue
            nload = switch.other_node(vout)
            if not nload or node_is_ground(nload):
                continue
            load_resistor = find_between(self.netlist.components_of("R"), "R", nload, gnd)
            if not load_resistor:
                continue
            control_node = switch.nodes[2] if len(switch.nodes) > 2 else ""
            control_source = None
            for comp in self.netlist.components_of("B"):
                if comp.nodes and comp.nodes[0] == control_node:
                    control_source = comp
                    break
            return switch, load_resistor, control_source
        return None, None, None

    def _recognize_control(self) -> ControlLoop:
        ramp = first_component_named(self.netlist, "Bramp") or find_b_source(self.netlist, "time/ts")
        error = first_component_named(self.netlist, "Berr") or find_b_source(self.netlist, "vout", "v(")
        int_src = first_component_named(self.netlist, "Bint") or find_b_source(self.netlist, "ki")
        int_cap = first_component_named(self.netlist, "Cint")
        int_leak = first_component_named(self.netlist, "Rint")
        der_src = first_component_named(self.netlist, "Bef") or find_b_source(self.netlist, "kf")
        der_cap = first_component_named(self.netlist, "Cef")
        der_leak = first_component_named(self.netlist, "Ref")
        duty = first_component_named(self.netlist, "Bctrl") or find_b_source(self.netlist, "limit", "kp")
        gate_hi = first_component_named(self.netlist, "Bgatehi") or find_b_source(self.netlist, "ctrl", "ramp")
        gate_lo = first_component_named(self.netlist, "Bgatelo") or find_b_source(self.netlist, "ctrl", "ramp")
        return ControlLoop(
            kind=self.netlist.annotations.control.get("type", "pid_pwm"),
            ramp=self._component_name(ramp),
            error=self._component_name(error),
            integrator_source=self._component_name(int_src),
            integrator_cap=self._component_name(int_cap),
            integrator_leak=self._component_name(int_leak),
            derivative_source=self._component_name(der_src),
            derivative_cap=self._component_name(der_cap),
            derivative_leak=self._component_name(der_leak),
            duty=self._component_name(duty),
            gate_hi=self._component_name(gate_hi),
            gate_lo=self._component_name(gate_lo),
        )

    @staticmethod
    def _component_name(comp: Optional[Component]) -> Optional[str]:
        return comp.name if comp else None


class CanonicalAscGenerator:
    def __init__(self, ir: VisualIR, layout: Optional[LayoutProfile] = None):
        self.ir = ir
        self.netlist = ir.netlist
        self.layout = layout or LayoutProfile.buck()
        self.lines: list[str] = []
        self.placed: set[str] = set()

    def generate(self) -> str:
        layout = self.layout
        self.uses_nmos_sw_body = False
        self.lines = [
            "Version 4",
            f"SHEET 1 {layout.sheet_width} {layout.sheet_height}",
            f"TEXT {layout.title_x} {layout.title_y} Left 3 ; Canonical {self.ir.power.topology.replace('_', ' ')} schematic",
            f"TEXT {layout.title_x} {layout.title_y + 40} Left 2 ; Generated from {self.netlist.path.name if self.netlist.path else 'netlist'} by netlist_to_canonical_asc.py",
            f"TEXT {layout.title_x} {layout.title_y + 72} Left 2 ; Layout profile: {layout.name}. Long formulas stay on hidden behavioral source values.",
            "",
        ]

        if self.ir.power.topology in {"synchronous_buck", "asynchronous_buck"}:
            self._emit_buck_template()
        elif self.ir.power.topology == "boost":
            self._emit_boost_template()
        elif self.ir.power.topology == "inverting_buck_boost":
            self._emit_inverting_buck_boost_template()
        else:
            self._emit_auxiliary_components(title="Unrecognized exact netlist")

        self._emit_control_template()
        self._emit_auxiliary_components(title="Additional exact netlist elements")
        self._emit_directives()
        return "\n".join(self.lines).rstrip() + "\n"

    def _component(self, name: Optional[str]) -> Optional[Component]:
        return self.netlist.by_name(name) if name else None

    def _emit_buck_template(self) -> None:
        p = self.ir.power
        layout = self.layout
        nodes = p.nodes
        vin = nodes.get("vin", "vin")
        sw = nodes.get("sw", "sw")
        lx = nodes.get("lx", "lx")
        vout = nodes.get("vout", "vout")
        gnd = nodes.get("gnd", "0")
        ncap = self._cap_node() or "ncap"
        nload = self._load_step_node() or "nload"
        load_ctl = self._load_control_node() or "load_ctl"

        ix = layout.input_x
        bx = layout.bridge_x
        mos_x = bx - 48
        bus_end_x = layout.bus_end_x
        top_y = layout.top_y
        phase_y = top_y + 192
        ground_y = layout.ground_y
        mos_pin_span = 96
        high_y = grid((top_y + phase_y) / 2 - mos_pin_span / 2)
        low_y = grid((phase_y + ground_y) / 2 - mos_pin_span / 2)
        high_drain_y = high_y
        high_source_y = high_y + mos_pin_span
        low_drain_y = low_y
        low_source_y = low_y + mos_pin_span
        ind_y = phase_y + 16
        label_y = top_y - 80
        lower_label_y = ground_y + 32
        power_line_end_x = bus_end_x
        series_centers = even_points(bx, power_line_end_x, 5 if p.load_switch and p.step_load else 4)
        lx_center = series_centers[0]
        dcr_center = series_centers[1]
        cap_center = series_centers[2]
        load_center = series_centers[3]
        step_center = series_centers[4] if len(series_centers) > 4 else grid(load_center + 256)
        lx_sym = grid(lx_center - 56)
        dcr_x = grid(dcr_center - 56)
        cap_x = grid(cap_center - 16)
        load_x = grid(load_center - 16)
        sload_x = step_center
        rstep_x = grid(step_center - 16)
        nload_y = phase_y + 80
        net_label_y = phase_y - 64

        vdc_y = two_pin_origin_for_center("voltage", "R0", (top_y + ground_y) // 2)
        vdc_top_y, vdc_bottom_y = [pt[1] for pt in pin_points("voltage", ix, vdc_y, "R0", 2)]

        cap_esr_y, cap_y = vertical_series_origins(phase_y, ground_y, [("res", "R0"), ("cap", "R0")])
        cap_esr_pins = pin_points("res", cap_x, cap_esr_y, "R0", 2)
        cap_pins = pin_points("cap", cap_x, cap_y, "R0", 2)
        cap_esr_top_y = min(cap_esr_pins[0][1], cap_esr_pins[1][1])
        cap_esr_bottom_y = max(cap_esr_pins[0][1], cap_esr_pins[1][1])
        cap_top_y = min(cap_pins[0][1], cap_pins[1][1])
        cap_bottom_y = max(cap_pins[0][1], cap_pins[1][1])

        fixed_y = two_pin_origin_for_center("res", "R0", (phase_y + ground_y) // 2)
        fixed_pins = pin_points("res", load_x, fixed_y, "R0", 2)
        fixed_top_y = min(fixed_pins[0][1], fixed_pins[1][1])
        fixed_bottom_y = max(fixed_pins[0][1], fixed_pins[1][1])

        rstep_y = two_pin_origin_for_center("res", "R0", (nload_y + ground_y) // 2)
        rstep_pins = pin_points("res", rstep_x, rstep_y, "R0", 2)
        rstep_top_y = min(rstep_pins[0][1], rstep_pins[1][1])
        rstep_bottom_y = max(rstep_pins[0][1], rstep_pins[1][1])
        sload_symbol_y = phase_y - 16
        sload_control_pins = pin_points("sw", sload_x, sload_symbol_y, "R0", 4)
        sload_ctl_pin_x, sload_ctl_pin_y = sload_control_pins[2]
        sload_ctl_gnd_x, sload_ctl_gnd_y = sload_control_pins[3]
        sload_label_x = sload_x - 224
        nload_label_x = rstep_x + 128
        sw_label_x = bx + 320
        sw_label_y = phase_y - 64
        ncap_label_x = cap_x - 160

        self.lines.extend(
            [
                f"TEXT {layout.title_x} {top_y - 72} Left 2 ; Input and synchronous power stage",
                f"WIRE {ix} {top_y} {bx} {top_y}",
                f"WIRE {ix} {top_y} {ix} {vdc_top_y}",
                f"WIRE {ix} {vdc_bottom_y} {ix} {ground_y}",
                f"WIRE {ix} {ground_y} {bus_end_x} {ground_y}",
                f"WIRE {bx} {top_y} {bx} {high_drain_y}",
                f"WIRE {bx} {high_source_y} {bx} {phase_y}",
                f"WIRE {bx} {phase_y} {lx_sym + 16} {phase_y}",
                f"WIRE {lx_sym + 96} {phase_y} {dcr_x + 16} {phase_y}",
                f"WIRE {dcr_x + 96} {phase_y} {bus_end_x} {phase_y}",
                f"WIRE {lx_sym + 96} {phase_y} {lx_sym + 96} {net_label_y}",
                f"WIRE {dcr_x + 96} {phase_y} {dcr_x + 96} {net_label_y}",
                f"WIRE {bx} {phase_y} {sw_label_x} {phase_y}",
                f"WIRE {sw_label_x} {phase_y} {sw_label_x} {sw_label_y}",
                f"WIRE {bx} {phase_y} {bx} {low_drain_y}",
                f"WIRE {bx} {low_source_y} {bx} {ground_y}",
                f"WIRE {cap_x + 16} {phase_y} {cap_x + 16} {cap_esr_top_y}",
                f"WIRE {cap_x + 16} {cap_esr_bottom_y} {cap_x + 16} {cap_top_y}",
                f"WIRE {cap_x + 16} {cap_top_y} {ncap_label_x} {cap_top_y}",
                f"WIRE {cap_x + 16} {cap_bottom_y} {cap_x + 16} {ground_y}",
                f"WIRE {load_x + 16} {phase_y} {load_x + 16} {fixed_top_y}",
                f"WIRE {load_x + 16} {fixed_bottom_y} {load_x + 16} {ground_y}",
                f"WIRE {sload_x} {phase_y} {sload_x} {phase_y + 80}",
                f"WIRE {sload_ctl_pin_x} {sload_ctl_pin_y} {sload_label_x} {sload_ctl_pin_y}",
                f"WIRE {sload_ctl_gnd_x} {sload_ctl_gnd_y} {sload_label_x} {sload_ctl_gnd_y}",
                f"WIRE {rstep_x + 16} {nload_y} {nload_label_x} {nload_y}",
                f"WIRE {rstep_x + 16} {nload_y} {rstep_x + 16} {rstep_top_y}",
                f"WIRE {rstep_x + 16} {rstep_bottom_y} {rstep_x + 16} {ground_y}",
                f"FLAG {ix} {top_y} {vin}",
                f"FLAG {sw_label_x} {sw_label_y} {sw}",
                f"FLAG {lx_sym + 96} {net_label_y} {lx}",
                f"FLAG {dcr_x + 96} {net_label_y} {vout}",
                f"FLAG {ncap_label_x} {cap_top_y} {ncap}",
                f"FLAG {nload_label_x} {nload_y} {nload}",
                f"FLAG {sload_label_x} {sload_ctl_pin_y} {load_ctl}",
                f"FLAG {sload_label_x} {sload_ctl_gnd_y} {gnd}",
                f"FLAG {ix} {ground_y} {gnd}",
                "",
            ]
        )

        self._emit_symbol(self._component(p.input_source), "voltage", ix, vdc_y)
        self._emit_label(ix + 56, (vdc_top_y + vdc_bottom_y) // 2 - 16, self._component_label(p.input_source, "Input Vdc"))
        self._emit_nmos_body_switch(self._component(p.high_switch), p.high_body_diode, mos_x, high_y)
        self._emit_nmos_body_switch(self._component(p.low_switch), p.low_body_diode, mos_x, low_y)
        self._flag_nmos_switch_control(mos_x, high_y, "gate_hi", gnd)
        self._flag_nmos_switch_control(mos_x, low_y, "gate_lo", gnd)
        self._emit_label(bx + 80, top_y - 112, "Half bridge")

        self._emit_symbol(self._component(p.switch_leak), "res", bx + 320, top_y + 256)
        self._flag_two_pin_offset(bx + 336, top_y + 272, bx + 336, top_y + 352, sw, gnd, dx=192)
        self._emit_label(bx + 260, top_y + 440, "switch leakage")

        self._emit_symbol(self._component(p.inductor), "ind", lx_sym, ind_y, rotation="R270")
        self._emit_symbol(self._component(p.inductor_esr), "res", dcr_x, ind_y, rotation="R270")
        self._emit_label(lx_sym + 8, label_y, self._component_label(p.inductor, "Output inductor"))
        self._emit_label(dcr_x + 8, label_y, self._component_label(p.inductor_esr, "Inductor DCR"))

        self._emit_symbol(self._component(p.cap_esr), "res", cap_x, cap_esr_y)
        self._emit_symbol(self._component(p.output_cap), "cap", cap_x, cap_y)
        self._emit_label(cap_x + 88, cap_top_y + 8, self._component_label(p.output_cap, "Output capacitor"))
        self._emit_label(cap_x + 88, cap_esr_top_y + 8, self._component_label(p.cap_esr, "Cap ESR"))

        self._emit_symbol(self._component(p.fixed_load), "res", load_x, fixed_y)
        self._emit_label(load_x + 72, (fixed_top_y + fixed_bottom_y) // 2 - 16, self._component_label(p.fixed_load, "Fixed load"))
        self._emit_symbol(self._component(p.load_switch), "sw", sload_x, sload_symbol_y)
        self._emit_symbol(self._component(p.step_load), "res", rstep_x, rstep_y)
        self._emit_label(sload_x - 520, ground_y + 96, "Switched load branch")
        self._emit_label(rstep_x + 72, (rstep_top_y + rstep_bottom_y) // 2 - 16, self._component_label(p.step_load, "Step load"))

        self.lines.extend(
            [
                f"TEXT {dcr_x + 112} {phase_y - 96} Left 2 ; output node: {vout}",
                f"TEXT {cap_x + 88} {lower_label_y} Left 2 ; probes: V({vout}) and I({p.inductor or 'L'})",
                f"TEXT {layout.title_x} {lower_label_y + 32} Left 2 ; Symmetry rule: components on each line sit at midpoint/even-division positions.",
                f"TEXT {layout.title_x} {lower_label_y + 64} Left 1 ; @symmetry {p.inductor or ''} {p.inductor_esr or ''} {p.output_cap or ''} {p.fixed_load or ''} {p.load_switch or ''}",
                "",
            ]
        )

    def _emit_boost_template(self) -> None:
        p = self.ir.power
        nodes = p.nodes
        vin = nodes.get("vin", "vin")
        sw = nodes.get("sw", "sw")
        vout = nodes.get("vout", "vout")
        gnd = nodes.get("gnd", "0")
        self.lines.extend(
            [
                "TEXT 96 152 Left 2 ; Boost power stage",
                "WIRE 128 192 448 192",
                "WIRE 528 192 608 192",
                "WIRE 688 192 1280 192",
                "WIRE 128 272 128 640",
                "WIRE 608 192 608 320",
                "WIRE 608 400 608 640",
                "WIRE 976 192 976 272",
                "WIRE 976 336 976 640",
                "WIRE 1120 192 1120 272",
                "WIRE 1120 272 1120 640",
                "WIRE 128 640 1280 640",
                f"FLAG 128 192 {vin}",
                f"FLAG 608 192 {sw}",
                f"FLAG 688 192 {vout}",
                f"FLAG 128 640 {gnd}",
                "",
            ]
        )
        self._emit_symbol(self._component(p.input_source), "voltage", 128, 176, label="Vdc")
        self._emit_symbol(self._component(p.inductor), "ind", 432, 208, rotation="R270", label="L")
        self._emit_symbol(self._component(p.main_switch), "sw", 608, 304, label="main switch")
        self._flag_switch_control(608, 304, "gate_hi", gnd)
        self._emit_symbol(self._component(p.freewheel_diode), "diode", 656, 128, rotation="R270", label="boost diode")
        self._emit_symbol(self._component(p.cap_esr), "res", 960, 176, label="ESR")
        self._emit_symbol(self._component(p.output_cap), "cap", 960, 272, label="Cout")
        self._emit_symbol(self._component(p.fixed_load), "res", 1104, 176, label="load")

    def _emit_inverting_buck_boost_template(self) -> None:
        p = self.ir.power
        nodes = p.nodes
        vin = nodes.get("vin", "vin")
        sw = nodes.get("sw", "sw")
        vout = nodes.get("vout", "vout")
        gnd = nodes.get("gnd", "0")
        self.lines.extend(
            [
                "TEXT 96 152 Left 2 ; Inverting buck-boost power stage",
                "WIRE 128 192 448 192",
                "WIRE 528 192 608 192",
                "WIRE 128 272 128 640",
                "WIRE 608 192 608 320",
                "WIRE 608 400 608 640",
                "WIRE 688 192 880 192",
                "WIRE 880 192 880 320",
                "WIRE 880 384 880 640",
                "WIRE 1040 320 1040 400",
                "WIRE 1040 400 1040 640",
                "WIRE 128 640 1200 640",
                f"FLAG 128 192 {vin}",
                f"FLAG 608 192 {sw}",
                f"FLAG 880 320 {vout}",
                f"FLAG 128 640 {gnd}",
                "",
            ]
        )
        self._emit_symbol(self._component(p.input_source), "voltage", 128, 176, label="Vdc")
        self._emit_symbol(self._component(p.inductor), "ind", 432, 208, rotation="R270", label="L")
        self._emit_symbol(self._component(p.main_switch), "sw", 608, 304, label="main switch")
        self._flag_switch_control(608, 304, "gate_hi", gnd)
        self._emit_symbol(self._component(p.freewheel_diode), "diode", 656, 128, rotation="R270", label="diode")
        self._emit_symbol(self._component(p.output_cap), "cap", 864, 320, label="Cout")
        self._emit_symbol(self._component(p.fixed_load), "res", 1024, 304, label="load")

    def _emit_control_template(self) -> None:
        c = self.ir.control
        layout = self.layout
        gnd = self.ir.power.nodes.get("gnd", "0")
        vout = self.ir.power.nodes.get("vout", "vout")
        y = layout.control_y
        step = layout.control_step_x
        xs = [layout.input_x + step * index for index in range(7)]
        gate_x = xs[-1] + step
        label_y = y - 72
        state_label_y = y + 208
        self.lines.extend(
            [
                f"TEXT {layout.title_x} {y - 128} Left 2 ; PWM and compensator abstraction",
                f"TEXT {layout.title_x + 288} {y - 128} Left 2 ; feedback uses V({vout})",
            ]
        )
        self._emit_symbol(self._component(c.ramp), "bv", xs[0], y, hide_value=True)
        self._flag_two_pin_offset(xs[0], y + 16, xs[0], y + 96, "ramp", gnd)
        self._emit_label(xs[0] + 56, label_y, self._component_label(c.ramp, "Carrier ramp"))

        self._emit_symbol(self._component(c.error), "bv", xs[1], y, hide_value=True)
        self._flag_two_pin_offset(xs[1], y + 16, xs[1], y + 96, "err", gnd)
        self._emit_label(xs[1] + 56, label_y, self._component_label(c.error, "Vref - Vout"))

        self._emit_symbol(self._component(c.integrator_source), "bi", xs[2], y + 16, hide_value=True)
        self._flag_two_pin_vertical_out(xs[2], y + 16, xs[2], y + 96, gnd, "int")
        self._emit_symbol(self._component(c.integrator_cap), "cap", xs[2] + 112, y)
        self._flag_two_pin_vertical_out(xs[2] + 128, y, xs[2] + 128, y + 64, "int", gnd)
        self._emit_symbol(self._component(c.integrator_leak), "res", xs[2] + 208, y)
        self._flag_two_pin_vertical_out(xs[2] + 224, y + 16, xs[2] + 224, y + 96, "int", gnd)
        self._emit_label(xs[2] + 56, state_label_y, "Integrator state")

        self._emit_symbol(self._component(c.derivative_source), "bi", xs[4], y + 16, hide_value=True)
        self._flag_two_pin_vertical_out(xs[4], y + 16, xs[4], y + 96, gnd, "ef")
        self._emit_symbol(self._component(c.derivative_cap), "cap", xs[4] + 112, y)
        self._flag_two_pin_vertical_out(xs[4] + 128, y, xs[4] + 128, y + 64, "ef", gnd)
        self._emit_symbol(self._component(c.derivative_leak), "res", xs[4] + 208, y)
        self._flag_two_pin_vertical_out(xs[4] + 224, y + 16, xs[4] + 224, y + 96, "ef", gnd)
        self._emit_label(xs[4] + 56, state_label_y, "Derivative filter state")

        self._emit_symbol(self._component(c.duty), "bv", xs[6], y, hide_value=True)
        self._flag_two_pin_offset(xs[6], y + 16, xs[6], y + 96, "ctrl", gnd)
        self._emit_label(xs[6] + 56, label_y, self._component_label(c.duty, "PID duty clamp"))

        self._emit_symbol(self._component(c.gate_hi), "bv", gate_x, y - 96, hide_value=True)
        self._flag_two_pin_offset(gate_x, y - 80, gate_x, y, "gate_hi", gnd)
        self._emit_label(gate_x + 56, y - 116, self._component_label(c.gate_hi, "PWM high gate"))
        self._emit_symbol(self._component(c.gate_lo), "bv", gate_x, y + 112, hide_value=True)
        self._flag_two_pin_offset(gate_x, y + 128, gate_x, y + 208, "gate_lo", gnd)
        self._emit_label(gate_x + 56, y + 252, self._component_label(c.gate_lo, "PWM low gate"))

        load_control = self._component(self.ir.power.load_control)
        if load_control:
            self._emit_symbol(load_control, "bv", layout.sload_x, layout.top_y + 384, hide_value=True)
            self._flag_two_pin_offset(layout.sload_x, layout.top_y + 400, layout.sload_x, layout.top_y + 480, load_control.nodes[0], gnd)
            self._emit_label(layout.sload_x + 64, layout.top_y + 536, self._component_label(load_control.name, "Load-step timing"))
        self.lines.extend(
            [
                f"TEXT {layout.title_x} {y + 320} Left 2 ; Behavioral formulas are retained on hidden source values; labels show the functional blocks.",
                "",
            ]
        )

    def _emit_auxiliary_components(self, title: str) -> None:
        remaining = [comp for comp in self.netlist.components if comp.name not in self.placed]
        if not remaining:
            return
        self.lines.append(f"TEXT {self.layout.title_x} {self.layout.aux_y} Left 2 ; {title}")
        x = self.layout.input_x
        y = self.layout.aux_y + 56
        for index, comp in enumerate(remaining):
            if index and index % 6 == 0:
                x = self.layout.input_x
                y += 160
            self._emit_generic_flagged(comp, x, y)
            x += 280
        self.lines.append("")

    def _emit_directives(self) -> None:
        self.lines.append(f"TEXT {self.layout.title_x} {self.layout.directive_y} Left 2 ; Simulation directives copied from the source netlist")
        y = self.layout.directive_y + 48
        if getattr(self, "uses_nmos_sw_body", False):
            for directive in (
                ".subckt nmos_sw_body D S G",
                "Ssw D S G 0 swmod",
                "Dbody S D dbody",
                ".ends nmos_sw_body",
            ):
                self.lines.append(f"TEXT {self.layout.title_x} {y} Left 2 !{directive}")
                y += 32
            y += 16
        for directive in self.netlist.directives:
            if directive.lower() == ".end":
                continue
            self.lines.append(f"TEXT {self.layout.title_x} {y} Left 2 !{directive}")
            y += 32
        self.lines.append("")

    def _emit_symbol(
        self,
        comp: Optional[Component],
        symbol: str,
        x: int,
        y: int,
        rotation: str = "R0",
        hide_value: bool = False,
        label: str = "",
    ) -> None:
        if not comp:
            if label:
                self._emit_label(x, y + 112, f"missing {label}")
            return
        value = comp.value
        self.lines.append(f"SYMBOL {symbol} {x} {y} {rotation}")
        if self.layout.hide_symbol_windows:
            self.lines.append("WINDOW 0 44 16 Invisible 2")
            self.lines.append("WINDOW 3 44 72 Invisible 2")
        else:
            self.lines.append("WINDOW 0 44 16 Left 2")
            if hide_value:
                self.lines.append("WINDOW 3 44 72 Invisible 2")
            else:
                self.lines.append("WINDOW 3 44 72 Left 2")
        self.lines.append(f"SYMATTR InstName {comp.name}")
        if value:
            self.lines.append(f"SYMATTR Value {value}")
        if comp.attrs:
            self.lines.append(f"SYMATTR SpiceLine {comp.attrs}")
        if label:
            self._emit_label(x + 56, y + 112, label)
        self.placed.add(comp.name)

    def _emit_nmos_body_switch(
        self,
        switch: Optional[Component],
        body_diode_ref: Optional[str],
        x: int,
        y: int,
    ) -> None:
        if not switch:
            self._emit_label(x, y + 112, "missing NMOS switch")
            return
        self.uses_nmos_sw_body = True
        self.lines.append(f"SYMBOL nmos_sw {x} {y} R0")
        self.lines.append("WINDOW 0 56 32 Invisible 2")
        self.lines.append("WINDOW 3 56 72 Invisible 2")
        self.lines.append(f"SYMATTR InstName X_{switch.name}")
        self.lines.append("SYMATTR Value nmos_sw_body")
        self.placed.add(switch.name)
        body_diode = self._component(body_diode_ref)
        if body_diode:
            self.placed.add(body_diode.name)

    def _emit_label(self, x: int, y: int, text: str, size: Optional[int] = None) -> None:
        text = text.replace("\n", " ").strip()
        if text:
            self.lines.append(f"TEXT {x} {y} Left {size or self.layout.label_size} ; {text}")

    def _component_label(self, comp_ref: Optional[str], fallback: str) -> str:
        comp = self._component(comp_ref)
        if not comp:
            return fallback
        if comp.kind in {"B", "S", "D"} or len(comp.value) > 18:
            return f"{comp.name}: {fallback}"
        return f"{comp.name} {comp.value}: {fallback}" if comp.value else f"{comp.name}: {fallback}"

    def _emit_generic_flagged(self, comp: Component, x: int, y: int) -> None:
        symbol = symbol_for_component(comp)
        rotation = "R0"
        self._emit_symbol(comp, symbol, x, y, rotation=rotation, hide_value=comp.kind in {"B", "E", "G"})
        pins = pin_points(symbol, x, y, rotation, len(comp.nodes))
        for node, (px, py) in zip(comp.nodes, pins):
            self.lines.append(f"FLAG {px} {py} {node}")

    def _flag_two_pin(self, x1: int, y1: int, x2: int, y2: int, node1: str, node2: str) -> None:
        self.lines.append(f"FLAG {x1} {y1} {node1}")
        self.lines.append(f"FLAG {x2} {y2} {node2}")

    def _flag_two_pin_offset(self, x1: int, y1: int, x2: int, y2: int, node1: str, node2: str, dx: int = -112) -> None:
        label_x1 = x1 + dx
        label_x2 = x2 + dx
        self.lines.append(f"WIRE {x1} {y1} {label_x1} {y1}")
        self.lines.append(f"WIRE {x2} {y2} {label_x2} {y2}")
        self.lines.append(f"FLAG {label_x1} {y1} {node1}")
        self.lines.append(f"FLAG {label_x2} {y2} {node2}")

    def _flag_two_pin_vertical_out(self, x1: int, y1: int, x2: int, y2: int, node1: str, node2: str, dy: int = 80) -> None:
        label_y1 = y1 - dy
        label_y2 = y2 + dy
        self.lines.append(f"WIRE {x1} {y1} {x1} {label_y1}")
        self.lines.append(f"WIRE {x2} {y2} {x2} {label_y2}")
        self.lines.append(f"FLAG {x1} {label_y1} {node1}")
        self.lines.append(f"FLAG {x2} {label_y2} {node2}")

    def _flag_switch_control(self, x: int, y: int, gate_node: str, ground_node: str) -> None:
        # LTspice's voltage-controlled switch symbol orders control+ at lower-left.
        self._flag_two_pin_offset(x - 48, y + 80, x - 48, y + 32, gate_node, ground_node, dx=-112)

    def _flag_nmos_switch_control(self, x: int, y: int, gate_node: str, ground_node: str) -> None:
        # nmos_sw exposes only D/S/G; control- is tied to 0 inside the subckt.
        self.lines.append(f"WIRE {x} {y + 80} {x - 176} {y + 80}")
        self.lines.append(f"FLAG {x - 176} {y + 80} {gate_node}")

    def _cap_node(self) -> Optional[str]:
        cap = self._component(self.ir.power.output_cap)
        if not cap:
            return None
        gnd = self.ir.power.nodes.get("gnd", "0")
        return cap.other_node(gnd)

    def _load_step_node(self) -> Optional[str]:
        step = self._component(self.ir.power.step_load)
        if not step:
            return None
        gnd = self.ir.power.nodes.get("gnd", "0")
        return step.other_node(gnd)

    def _load_control_node(self) -> Optional[str]:
        switch = self._component(self.ir.power.load_switch)
        if switch and len(switch.nodes) >= 3:
            return switch.nodes[2]
        control = self._component(self.ir.power.load_control)
        if control and control.nodes:
            return control.nodes[0]
        return None


def symbol_for_component(comp: Component) -> str:
    if comp.kind == "R":
        return "res"
    if comp.kind == "C":
        return "cap"
    if comp.kind == "L":
        return "ind"
    if comp.kind == "V":
        return "voltage"
    if comp.kind == "I":
        return "current"
    if comp.kind == "D":
        return "diode"
    if comp.kind == "S":
        return "sw"
    if comp.kind == "B":
        return "bi" if comp.value.strip().upper().startswith("I=") else "bv"
    if comp.kind == "E":
        return "e"
    return "bv"


def pin_points(symbol: str, x: int, y: int, rotation: str, node_count: int) -> list[tuple[int, int]]:
    offsets: dict[tuple[str, str], list[tuple[int, int]]] = {
        ("voltage", "R0"): [(0, 16), (0, 96)],
        ("current", "R0"): [(0, 16), (0, 96)],
        ("bv", "R0"): [(0, 16), (0, 96)],
        ("bi", "R0"): [(0, 0), (0, 80)],
        ("res", "R0"): [(16, 16), (16, 96)],
        ("res", "R270"): [(16, -16), (96, -16)],
        ("cap", "R0"): [(16, 0), (16, 64)],
        ("ind", "R0"): [(16, 16), (16, 96)],
        ("ind", "R270"): [(16, -16), (96, -16)],
        ("diode", "R0"): [(16, 0), (16, 64)],
        ("diode", "R270"): [(16, -16), (80, -16)],
        ("sw", "R0"): [(0, 16), (0, 96), (-48, 80), (-48, 32)],
        ("nmos_sw", "R0"): [(48, 0), (48, 96), (0, 80)],
        ("e", "R0"): [(0, 16), (0, 96)],
    }
    selected = offsets.get((symbol, rotation)) or offsets.get((symbol, "R0")) or [(0, 16), (0, 96)]
    return [(x + dx, y + dy) for dx, dy in selected[:node_count]]


def parse_asc(asc_text: str) -> ParsedAsc:
    parsed = ParsedAsc(sheet_width=2000, sheet_height=1400)
    current_symbol: Optional[AscSymbol] = None
    for raw_line in asc_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("SHEET "):
            parts = line.split()
            if len(parts) >= 4:
                parsed.sheet_width = int(parts[2])
                parsed.sheet_height = int(parts[3])
            current_symbol = None
        elif line.startswith("WIRE "):
            parts = line.split()
            if len(parts) >= 5:
                parsed.wires.append(tuple(int(part) for part in parts[1:5]))  # type: ignore[arg-type]
            current_symbol = None
        elif line.startswith("FLAG "):
            parts = line.split(None, 3)
            if len(parts) >= 4:
                parsed.flags.append((int(parts[1]), int(parts[2]), parts[3]))
            current_symbol = None
        elif line.startswith("TEXT "):
            match = re.match(r"TEXT\s+(-?\d+)\s+(-?\d+)\s+\S+\s+(\d+)\s*(.*)$", line)
            if match:
                payload = match.group(4).strip()
                source = "comment"
                if payload.startswith("!"):
                    source = "directive"
                    payload = payload[1:].strip()
                elif payload.startswith(";"):
                    payload = payload[1:].strip()
                parsed.texts.append(
                    AscText(
                        x=int(match.group(1)),
                        y=int(match.group(2)),
                        size=int(match.group(3)),
                        text=payload,
                        source=source,
                    )
                )
                if payload.startswith("@symmetry"):
                    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", payload)
                    parsed.symmetry_lines.append([name for name in names if name != "symmetry"])
            current_symbol = None
        elif line.startswith("SYMBOL "):
            parts = line.split()
            if len(parts) >= 5:
                current_symbol = AscSymbol(
                    name=parts[1],
                    x=int(parts[2]),
                    y=int(parts[3]),
                    rotation=parts[4],
                )
                parsed.symbols.append(current_symbol)
        elif current_symbol and line.startswith("WINDOW "):
            parts = line.split()
            if len(parts) >= 5:
                window_id = int(parts[1])
                visible = "Invisible" not in parts[4:]
                current_symbol.windows[window_id] = (int(parts[2]), int(parts[3]), visible)
        elif current_symbol and line.startswith("SYMATTR InstName "):
            current_symbol.inst_name = line.replace("SYMATTR InstName ", "", 1).strip()
        elif current_symbol and line.startswith("SYMATTR Value "):
            current_symbol.value = line.replace("SYMATTR Value ", "", 1).strip()
    return parsed


def symbol_body_box(symbol: AscSymbol) -> Box:
    bounds: dict[tuple[str, str], tuple[int, int, int, int]] = {
        ("voltage", "R0"): (-40, 0, 40, 112),
        ("current", "R0"): (-40, 0, 40, 112),
        ("bv", "R0"): (-40, 0, 80, 112),
        ("bi", "R0"): (-40, -16, 80, 96),
        ("sw", "R0"): (-56, 8, 64, 104),
        ("res", "R0"): (-12, 8, 52, 104),
        ("cap", "R0"): (-12, 0, 52, 80),
        ("diode", "R0"): (-16, -12, 56, 84),
        ("ind", "R0"): (-16, 0, 56, 112),
        ("res", "R270"): (0, -56, 128, 24),
        ("ind", "R270"): (0, -64, 128, 32),
        ("diode", "R270"): (0, -56, 112, 24),
        ("nmos_sw", "R0"): (0, 0, 112, 96),
        ("e", "R0"): (-40, 0, 80, 112),
    }
    dx1, dy1, dx2, dy2 = bounds.get((symbol.name, symbol.rotation), bounds.get((symbol.name, "R0"), (-32, -16, 96, 112)))
    ident = symbol.inst_name or symbol.name
    return Box(symbol.x + dx1, symbol.y + dy1, symbol.x + dx2, symbol.y + dy2, "symbol", ident, owner=ident)


def text_box(text: AscText, ident: str = "") -> Box:
    char_width = 5 + text.size * 2
    height = 9 + text.size * 4
    width = max(28, len(text.text) * char_width)
    return Box(text.x, text.y - height + 4, text.x + width, text.y + 6, "text", ident or text.text[:48], owner=ident)


def window_text_boxes(symbol: AscSymbol) -> list[Box]:
    boxes: list[Box] = []
    owner = symbol.inst_name or symbol.name
    contents = {0: symbol.inst_name, 3: symbol.value}
    for window_id, content in contents.items():
        if not content:
            continue
        window = symbol.windows.get(window_id)
        if not window:
            continue
        dx, dy, visible = window
        if not visible:
            continue
        asc_text = AscText(symbol.x + dx, symbol.y + dy, 2, content, "window")
        box = text_box(asc_text, ident=f"{owner}:window{window_id}")
        boxes.append(Box(box.x1, box.y1, box.x2, box.y2, "text", box.ident, owner=owner))
    return boxes


def wire_box(wire: tuple[int, int, int, int], padding: int = 4) -> Box:
    x1, y1, x2, y2 = wire
    return Box(min(x1, x2) - padding, min(y1, y2) - padding, max(x1, x2) + padding, max(y1, y2) + padding, "wire", f"wire:{wire}")


def geometry_boxes(parsed: ParsedAsc) -> tuple[list[Box], list[Box], list[Box], list[Box]]:
    symbol_boxes = [symbol_body_box(symbol) for symbol in parsed.symbols]
    explicit_text_boxes = [
        text_box(text, ident=f"{text.source}:{index}:{text.text[:32]}") for index, text in enumerate(parsed.texts)
    ]
    window_boxes: list[Box] = []
    for symbol in parsed.symbols:
        window_boxes.extend(window_text_boxes(symbol))
    wire_boxes = [wire_box(wire) for wire in parsed.wires]
    flag_boxes = [
        Box(x - 8, y - 8, x + 8 + max(8, len(name) * 7), y + 12, "flag", name)
        for x, y, name in parsed.flags
        if name != "0"
    ]
    text_boxes = explicit_text_boxes + window_boxes
    return symbol_boxes, text_boxes, wire_boxes, flag_boxes


class VisualScorer:
    def score(self, parsed: ParsedAsc) -> VisualScore:
        symbol_boxes, text_boxes, wire_boxes, flag_boxes = geometry_boxes(parsed)
        node_text_boxes = text_boxes + flag_boxes
        issues: list[VisualIssue] = []

        for box in symbol_boxes + node_text_boxes:
            if box.x1 < 0 or box.y1 < 0 or box.x2 > parsed.sheet_width or box.y2 > parsed.sheet_height:
                severity = 200 + box.intersection_area(Box(-10000, -10000, parsed.sheet_width, parsed.sheet_height, "sheet", "sheet"))
                issues.append(
                    VisualIssue(
                        kind="out_of_bounds",
                        severity=severity,
                        message=f"{box.kind} {box.ident} extends outside the sheet",
                        objects=[box.ident],
                        box=(box.x1, box.y1, box.x2, box.y2),
                    )
                )

        self._pairwise(node_text_boxes, node_text_boxes, "text_text_overlap", 35, issues, skip_same_owner=True, padding=0)
        self._pairwise(symbol_boxes, symbol_boxes, "symbol_symbol_overlap", 80, issues, skip_same_owner=True, padding=0)
        self._pairwise(node_text_boxes, symbol_boxes, "text_symbol_overlap", 55, issues, skip_same_owner=True, padding=2)

        for text in text_boxes:
            if text.ident.startswith("directive:"):
                continue
            if "@symmetry" in text.ident:
                continue
            padded_text = text.expanded(2)
            for wire in wire_boxes:
                if padded_text.intersects(wire):
                    area = padded_text.intersection_area(wire)
                    severity = 45 + area
                    issues.append(
                        VisualIssue(
                            kind="text_wire_overlap",
                            severity=severity,
                            message=f"text {text.ident} overlaps a wire",
                            objects=[text.ident, wire.ident],
                            box=(
                                max(padded_text.x1, wire.x1),
                                max(padded_text.y1, wire.y1),
                                min(padded_text.x2, wire.x2),
                                min(padded_text.y2, wire.y2),
                            ),
                        )
                    )

        self._symmetry(parsed, symbol_boxes, issues)

        collisions = sum(1 for issue in issues if issue.kind != "out_of_bounds")
        out_of_bounds = sum(1 for issue in issues if issue.kind == "out_of_bounds")
        score = sum(issue.severity for issue in issues) + collisions * 25 + out_of_bounds * 100
        return VisualScore(score=score, collisions=collisions, out_of_bounds=out_of_bounds, issues=issues)

    @staticmethod
    def _symmetry(parsed: ParsedAsc, symbol_boxes: list[Box], issues: list[VisualIssue]) -> None:
        boxes_by_name = {box.ident: box for box in symbol_boxes}
        for group in parsed.symmetry_lines:
            names = [name for name in group if name in boxes_by_name]
            if len(names) < 2:
                continue
            centers = [((boxes_by_name[name].x1 + boxes_by_name[name].x2) / 2.0, name) for name in names]
            centers.sort()
            gaps = [centers[index + 1][0] - centers[index][0] for index in range(len(centers) - 1)]
            if not gaps:
                continue
            target = sum(gaps) / len(gaps)
            max_error = max(abs(gap - target) for gap in gaps)
            if max_error <= 24:
                continue
            severity = int(60 + max_error * 4)
            issues.append(
                VisualIssue(
                    kind="symmetry_spacing",
                    severity=severity,
                    message=f"components are not evenly spaced: {', '.join(names)}",
                    objects=names,
                    box=(
                        int(min(boxes_by_name[name].x1 for name in names)),
                        int(min(boxes_by_name[name].y1 for name in names)),
                        int(max(boxes_by_name[name].x2 for name in names)),
                        int(max(boxes_by_name[name].y2 for name in names)),
                    ),
                )
            )

    @staticmethod
    def _pairwise(
        left: list[Box],
        right: list[Box],
        kind: str,
        base_severity: int,
        issues: list[VisualIssue],
        skip_same_owner: bool = False,
        padding: int = 4,
    ) -> None:
        same_list = left is right
        for i, box_a in enumerate(left):
            start = i + 1 if same_list else 0
            for box_b in right[start:]:
                if skip_same_owner and box_a.owner and box_a.owner == box_b.owner:
                    continue
                padded_a = box_a.expanded(padding)
                padded_b = box_b.expanded(padding)
                if not padded_a.intersects(padded_b):
                    continue
                area = padded_a.intersection_area(padded_b)
                severity = base_severity + max(1, area // 8)
                issues.append(
                    VisualIssue(
                        kind=kind,
                        severity=severity,
                        message=f"{box_a.ident} overlaps {box_b.ident}",
                        objects=[box_a.ident, box_b.ident],
                        box=(
                            max(padded_a.x1, padded_b.x1),
                            max(padded_a.y1, padded_b.y1),
                            min(padded_a.x2, padded_b.x2),
                            min(padded_a.y2, padded_b.y2),
                        ),
                    )
                )


class AscPreviewRenderer:
    def __init__(self, scale: float = 0.5):
        self.scale = scale

    def render(self, parsed: ParsedAsc, output_path: Path, score: Optional[VisualScore] = None) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:  # pragma: no cover - covered by CLI behavior when Pillow is absent.
            raise RuntimeError("Pillow is required for visual previews") from exc

        scale = self.scale

        def pt(x: int, y: int) -> tuple[int, int]:
            return int(x * scale), int(y * scale)

        image = Image.new("RGB", pt(parsed.sheet_width, parsed.sheet_height), (206, 206, 206))
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("arial.ttf", max(10, int(18 * scale)))
            title_font = ImageFont.truetype("arial.ttf", max(12, int(24 * scale)))
        except OSError:
            font = ImageFont.load_default()
            title_font = font

        for wire in parsed.wires:
            draw.line([pt(wire[0], wire[1]), pt(wire[2], wire[3])], fill=(20, 20, 20), width=max(1, int(3 * scale)))

        symbol_boxes, text_boxes, _, flag_boxes = geometry_boxes(parsed)
        for symbol, box in zip(parsed.symbols, symbol_boxes):
            if symbol.name == "nmos_sw" and symbol.rotation == "R0":
                self._draw_nmos(draw, pt, symbol, scale)
            else:
                draw.rectangle([pt(box.x1, box.y1), pt(box.x2, box.y2)], outline=(30, 60, 210), width=max(1, int(2 * scale)))

        for flag in flag_boxes:
            draw.text(pt(flag.x1, flag.y1 + 8), flag.ident, fill=(0, 0, 120), font=font)

        for symbol in parsed.symbols:
            for box in window_text_boxes(symbol):
                draw.text(pt(box.x1, box.y2 - 6), box.ident.split(":window", 1)[0], fill=(0, 0, 160), font=font)

        for text in parsed.texts:
            used_font = title_font if text.size >= 3 else font
            draw.text(pt(text.x, text.y - 14), text.text, fill=(0, 0, 160), font=used_font)

        if score:
            for issue in score.issues[:80]:
                x1, y1, x2, y2 = issue.box
                draw.rectangle([pt(x1, y1), pt(x2, y2)], outline=(210, 40, 40), width=max(1, int(3 * scale)))

        image.save(output_path)

    @staticmethod
    def _draw_nmos(draw, pt, symbol: AscSymbol, scale: float) -> None:
        x = symbol.x
        y = symbol.y
        width = max(1, int(2 * scale))
        color = (30, 60, 210)
        lines = [
            (48, 80, 48, 96),
            (16, 80, 48, 80),
            (40, 48, 48, 48),
            (16, 8, 16, 24),
            (16, 40, 16, 56),
            (16, 72, 16, 88),
            (0, 80, 8, 80),
            (8, 16, 8, 80),
            (48, 16, 16, 16),
            (48, 0, 48, 16),
            (48, 16, 88, 16),
            (48, 80, 88, 80),
            (72, 60, 104, 60),
            (72, 36, 104, 36),
            (88, 16, 88, 36),
            (88, 60, 88, 80),
        ]
        for x1, y1, x2, y2 in lines:
            draw.line([pt(x + x1, y + y1), pt(x + x2, y + y2)], fill=color, width=width)
        draw.line([pt(x + 16, y + 48), pt(x + 40, y + 44)], fill=color, width=width)
        draw.line([pt(x + 16, y + 48), pt(x + 40, y + 52)], fill=color, width=width)
        draw.line([pt(x + 40, y + 44), pt(x + 40, y + 52)], fill=color, width=width)
        draw.line([pt(x + 72, y + 60), pt(x + 88, y + 36)], fill=color, width=width)
        draw.line([pt(x + 104, y + 60), pt(x + 88, y + 36)], fill=color, width=width)


@dataclass
class VisualAgentResult:
    asc_text: str
    profile: LayoutProfile
    score: VisualScore
    report: dict[str, object]


@dataclass
class VisionReviewResult:
    model: str
    review: dict[str, object]
    raw_text: str

    def to_dict(self) -> dict[str, object]:
        return {"model": self.model, "review": self.review, "raw_text": self.raw_text}


class OpenAIVisionReviewer:
    """Optional VLM pass for subjective schematic readability review."""

    def __init__(self, model: str = "gpt-4.1", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required when --vision-review is enabled")

    def review(
        self,
        preview_path: Path,
        topology: str,
        visual_score: VisualScore,
        reference_image_path: Optional[Path] = None,
    ) -> VisionReviewResult:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - local environment currently has openai installed.
            raise RuntimeError("The openai Python package is required for --vision-review") from exc

        client = OpenAI(api_key=self.api_key)
        content: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": self._prompt(topology, visual_score, bool(reference_image_path)),
            },
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{self._encode_image(preview_path)}",
            },
        ]
        if reference_image_path:
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{self._encode_image(reference_image_path)}",
                }
            )

        response = client.responses.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
        )
        raw_text = getattr(response, "output_text", "") or str(response)
        review = self._parse_review(raw_text)
        return VisionReviewResult(model=self.model, review=review, raw_text=raw_text)

    @staticmethod
    def _encode_image(path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode("ascii")

    @staticmethod
    def _prompt(topology: str, visual_score: VisualScore, has_reference: bool) -> str:
        reference_text = (
            "The second image is a target/reference schematic style. Compare against it explicitly."
            if has_reference
            else "No reference image is attached; compare against a standard textbook/Simulink-style synchronous buck schematic."
        )
        return f"""You are reviewing an automatically generated LTspice schematic preview.
The rule-based layout pass reports score={visual_score.score}, hard_collisions={visual_score.collisions}, out_of_bounds={visual_score.out_of_bounds}.
Topology: {topology}.
{reference_text}

Evaluate subjective readability, not simulation correctness. Return JSON only with this exact shape:
{{
  "readability_score": 1-10,
  "topology_similarity_score": 1-10,
  "verdict": "pass" | "needs_revision",
  "strengths": ["short bullets"],
  "problems": ["short bullets"],
  "layout_actions": [
    {{"area": "power stage|control loop|labels|probes|overall", "action": "specific coordinate/layout guidance"}}
  ]
}}

Pass only if a power electronics engineer could identify input, half bridge, output filter, load, and control loop within a few seconds."""

    @staticmethod
    def _parse_review(raw_text: str) -> dict[str, object]:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            return normalize_vision_review(parsed if isinstance(parsed, dict) else {"parsed": parsed})
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    return normalize_vision_review(parsed if isinstance(parsed, dict) else {"parsed": parsed})
                except json.JSONDecodeError:
                    pass
        return normalize_vision_review({"error": "Could not parse model output as JSON", "text": raw_text})


def normalize_vision_review(review: dict[str, object]) -> dict[str, object]:
    """Make provider outputs comparable even when they omit optional fields."""
    normalized = dict(review)
    for key in ("readability_score", "topology_similarity_score"):
        value = normalized.get(key)
        if isinstance(value, (int, float)) and 0 < value <= 1:
            normalized[key] = round(value * 10, 1)
    normalized.setdefault("strengths", [])
    normalized.setdefault("problems", [])
    normalized.setdefault("layout_actions", [])
    if "verdict" not in normalized:
        text = str(normalized.get("text", "")).lower()
        normalized["verdict"] = "pass" if "highly effective" in text or "final verdict" in text and "effective" in text else "needs_revision"
    return normalized


class MiniMaxVisionReviewer:
    """Optional MiniMax reviewer using the official mmx vision CLI."""

    def __init__(
        self,
        model: str = "mmx-vision",
        api_key: Optional[str] = None,
        api_host: Optional[str] = None,
        command: str = "mmx",
    ):
        self.model = model
        self.api_key = api_key or os.environ.get("MINIMAX_API_KEY")
        self.api_host = (api_host or os.environ.get("MINIMAX_API_HOST") or "https://api.minimax.io").rstrip("/")
        self.command = command
        if not self.api_key:
            raise RuntimeError("MINIMAX_API_KEY is required when --vision-provider minimax is enabled")

    def review(
        self,
        preview_path: Path,
        topology: str,
        visual_score: VisualScore,
        reference_image_path: Optional[Path] = None,
    ) -> VisionReviewResult:
        prompt = self._prompt(topology, visual_score, bool(reference_image_path))
        image_path = preview_path.resolve()
        if reference_image_path:
            prompt += f"\n\nReference image path for comparison: {reference_image_path.resolve()}"

        command = [
            self._resolve_command(),
            "vision",
            "describe",
            "--image",
            str(image_path),
            "--prompt",
            prompt,
            "--output",
            "json",
            "--non-interactive",
            "--quiet",
            "--timeout",
            "300",
        ]
        if self.api_host:
            command.extend(["--base-url", self.api_host])
        if self.api_key:
            command.extend(["--api-key", self.api_key])

        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=360,
        )
        if completed.returncode != 0:
            stderr = self._redact(completed.stderr.strip())
            stdout = self._redact(completed.stdout.strip())
            raise RuntimeError(f"MiniMax mmx vision failed with exit code {completed.returncode}: {stderr or stdout}")

        raw_text = self._extract_cli_text(completed.stdout)
        review = OpenAIVisionReviewer._parse_review(self._strip_thinking(raw_text))
        return VisionReviewResult(model=self.model, review=review, raw_text=raw_text)

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "<MINIMAX_API_KEY>") if self.api_key else text

    def _resolve_command(self) -> str:
        resolved = shutil.which(self.command)
        if resolved:
            return resolved
        if os.name == "nt" and not self.command.lower().endswith((".cmd", ".exe", ".bat")):
            for suffix in (".cmd", ".exe", ".bat"):
                resolved = shutil.which(self.command + suffix)
                if resolved:
                    return resolved
        return self.command

    @staticmethod
    def _strip_thinking(text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    @staticmethod
    def _prompt(topology: str, visual_score: VisualScore, has_reference: bool) -> str:
        reference = " Compare it to the reference image too." if has_reference else ""
        return (
            f"Review this LTspice {topology} schematic preview for engineering readability."
            f" Rule checker reports score={visual_score.score}, collisions={visual_score.collisions}, "
            f"out_of_bounds={visual_score.out_of_bounds}.{reference} "
            'Return ONLY valid JSON with keys: "readability_score" (1-10), '
            '"topology_similarity_score" (1-10), "verdict" ("pass" or "needs_revision"), '
            '"strengths" (array of strings), "problems" (array of strings), '
            '"layout_actions" (array of {"area": string, "action": string}). No markdown.'
        )

    @staticmethod
    def _extract_cli_text(stdout: str) -> str:
        text = stdout.strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("content", "text", "message", "result", "description", "output"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(data, ensure_ascii=False)
        return text


class VisualQAAgent:
    def __init__(self, ir: VisualIR, max_iterations: int = 20):
        self.ir = ir
        self.max_iterations = max_iterations
        self.scorer = VisualScorer()

    def run(self, preview_path: Optional[Path] = None, report_path: Optional[Path] = None) -> VisualAgentResult:
        best: Optional[tuple[str, LayoutProfile, VisualScore]] = None
        iterations: list[dict[str, object]] = []
        for profile in self._candidate_profiles()[: self.max_iterations]:
            asc_text = CanonicalAscGenerator(self.ir, layout=profile).generate()
            parsed = parse_asc(asc_text)
            score = self.scorer.score(parsed)
            iterations.append(
                {
                    "profile": asdict(profile),
                    "score": score.score,
                    "collisions": score.collisions,
                    "out_of_bounds": score.out_of_bounds,
                }
            )
            if best is None or score.score < best[2].score:
                best = (asc_text, profile, score)
            if score.score == 0:
                break

        if best is None:
            raise RuntimeError("VisualQAAgent did not produce any layout candidates")

        asc_text, profile, score = best
        parsed = parse_asc(asc_text)
        report = {
            "selected_profile": asdict(profile),
            "score": score.to_dict(),
            "iterations": iterations,
        }
        if preview_path:
            AscPreviewRenderer().render(parsed, preview_path, score=score)
            report["preview"] = str(preview_path)
        if report_path:
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
        return VisualAgentResult(asc_text=asc_text, profile=profile, score=score, report=report)

    def _candidate_profiles(self) -> list[LayoutProfile]:
        if self.ir.power.topology not in {"synchronous_buck", "asynchronous_buck"}:
            return [LayoutProfile.buck()]

        profiles: list[LayoutProfile] = []
        for x_scale in [1.0, 1.12, 1.25, 1.38, 1.5]:
            for ground_y in [752, 816]:
                for control_y in [1008, 1120]:
                    profiles.append(LayoutProfile.buck(x_scale=x_scale, control_y=control_y, ground_y=ground_y))
        return profiles


def build_ir(path: Path, topology_override: Optional[str] = None) -> VisualIR:
    netlist = parse_netlist(path)
    return TopologyRecognizer(netlist, topology_override=topology_override).recognize()


def ir_to_jsonable(ir: VisualIR) -> dict[str, object]:
    data = asdict(ir)
    if ir.netlist.path:
        data["netlist"]["path"] = str(ir.netlist.path)
    return data


def default_output_path(source: Path) -> Path:
    suffix = source.suffix or ".cir"
    stem = source.name[: -len(suffix)] if source.name.endswith(suffix) else source.stem
    return source.with_name(f"{stem}_canonical.asc")


def main(argv: Optional[list[str]] = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Input SPICE .cir/.net file")
    parser.add_argument("-o", "--output", type=Path, help="Output LTspice .asc file")
    parser.add_argument(
        "--topology",
        choices=["synchronous_buck", "asynchronous_buck", "boost", "inverting_buck_boost"],
        help="Override or supply topology when annotations are absent",
    )
    parser.add_argument("--dump-ir", action="store_true", help="Print recognized semantic IR as JSON")
    parser.add_argument("--visual-agent", action="store_true", help="Iterate layouts using the built-in visual scorer")
    parser.add_argument("--preview", type=Path, help="Write a PNG preview for the selected layout")
    parser.add_argument("--visual-report", type=Path, help="Write visual scoring details as JSON")
    parser.add_argument("--max-visual-iterations", type=int, default=20, help="Maximum layout candidates to evaluate")
    parser.add_argument("--vision-review", action="store_true", help="Send the generated preview PNG to an OpenAI vision model for readability review")
    parser.add_argument(
        "--vision-provider",
        choices=["openai", "minimax"],
        default=os.environ.get("VISION_PROVIDER", "openai"),
        help="Vision reviewer provider",
    )
    parser.add_argument(
        "--vision-model",
        default=os.environ.get("VISION_REVIEW_MODEL"),
        help="Vision model name. Defaults to gpt-4.1 for OpenAI and mmx-vision for MiniMax CLI.",
    )
    parser.add_argument("--reference-image", type=Path, help="Optional reference schematic image for the vision reviewer")
    args = parser.parse_args(argv)

    ir = build_ir(args.source, topology_override=args.topology)
    if args.dump_ir:
        print(json.dumps(ir_to_jsonable(ir), indent=2))

    output = args.output or default_output_path(args.source)
    report: Optional[dict[str, object]] = None
    final_score: Optional[VisualScore] = None
    if args.visual_agent:
        result = VisualQAAgent(ir, max_iterations=args.max_visual_iterations).run(
            preview_path=args.preview,
            report_path=None,
        )
        asc_text = result.asc_text
        report = result.report
        final_score = result.score
        print(
            f"Visual agent selected {result.profile.name}: "
            f"score={result.score.score}, collisions={result.score.collisions}, out_of_bounds={result.score.out_of_bounds}"
        )
    else:
        asc_text = CanonicalAscGenerator(ir).generate()
        parsed = parse_asc(asc_text)
        final_score = VisualScorer().score(parsed)
        report = {"selected_profile": asdict(LayoutProfile.buck()), "score": final_score.to_dict(), "iterations": []}
        if args.preview:
            AscPreviewRenderer().render(parsed, args.preview, score=final_score)
            report["preview"] = str(args.preview)
            print(f"Wrote preview {args.preview} (score={final_score.score}, collisions={final_score.collisions})")

    if args.vision_review:
        preview_path = args.preview or output.with_suffix(".png")
        if not preview_path.exists():
            AscPreviewRenderer().render(parse_asc(asc_text), preview_path, score=final_score)
            if report is not None:
                report["preview"] = str(preview_path)
        if args.vision_provider == "minimax":
            reviewer = MiniMaxVisionReviewer(model=args.vision_model or "mmx-vision")
        else:
            reviewer = OpenAIVisionReviewer(model=args.vision_model or "gpt-4.1")
        review = reviewer.review(
            preview_path=preview_path,
            topology=ir.topology,
            visual_score=final_score or VisualScorer().score(parse_asc(asc_text)),
            reference_image_path=args.reference_image,
        )
        if report is not None:
            report["vision_provider"] = args.vision_provider
            report["vision_review"] = review.to_dict()
        print(
            f"Vision review ({args.vision_provider}/{review.model}): "
            f"verdict={review.review.get('verdict', 'unknown')}, "
            f"readability={review.review.get('readability_score', 'unknown')}"
        )

    if args.visual_report and report is not None:
        args.visual_report.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    output.write_text(asc_text, encoding="utf-8", newline="\n")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
