"""
Adapter: JSON sidecar dict -> DesignData.

This is the lightweight, tool-agnostic format documented on
``pcb_dfm.ingest.design_data``. It is the simplest way to supply stackup /
controlled-impedance / net info and is what the correctness tests use::

    {
      "stackup": {
        "er": 4.3,
        "dielectric_thickness_mm": 0.20,
        "copper_thickness_mm": 0.035,
        "dielectric_layers_mm": [0.10, 0.20, 0.20, 0.10]
      },
      "controlled_impedance": [
        {"name": "USB_DP", "width_mm": 0.20, "target_ohm": 90, "tolerance_pct": 10}
      ],
      "nets": {
        "USB_DP": {"routed_length_mm": 51.2, "net_class": "USB"},
        "USB_DN": {"routed_length_mm": 50.9, "net_class": "USB"}
      },
      "diff_pairs": [
        {"name": "USB", "positive": "USB_DP", "negative": "USB_DN", "target_ohm": 90}
      ]
    }
"""

from __future__ import annotations

from typing import Any, Dict

from ..design_model import (
    ControlledImpedanceSpec,
    DesignData,
    DiffPair,
    Net,
    NetFeature,
    Stackup,
    StackupLayer,
)


def _stackup_from_dict(d: Dict[str, Any]) -> Stackup:
    """Build a Stackup from the flat sidecar stackup dict.

    A single dielectric/copper pair is synthesized from the scalar fields, and
    each entry in ``dielectric_layers_mm`` becomes its own dielectric layer, so
    the Stackup's representative er/thickness properties reproduce the values
    the sidecar provided.
    """
    layers = []
    t_cu = d.get("copper_thickness_mm")
    if isinstance(t_cu, (int, float)):
        layers.append(StackupLayer(name="copper", kind="copper", thickness_mm=float(t_cu)))

    er = d.get("er")
    layer_list = d.get("dielectric_layers_mm")
    if isinstance(layer_list, list) and layer_list:
        for i, th in enumerate(layer_list):
            if isinstance(th, (int, float)):
                layers.append(StackupLayer(
                    name=f"dielectric_{i + 1}", kind="dielectric",
                    thickness_mm=float(th),
                    er=float(er) if isinstance(er, (int, float)) else None,
                ))
    else:
        th = d.get("dielectric_thickness_mm")
        if isinstance(th, (int, float)) or isinstance(er, (int, float)):
            layers.append(StackupLayer(
                name="dielectric", kind="dielectric",
                thickness_mm=float(th) if isinstance(th, (int, float)) else None,
                er=float(er) if isinstance(er, (int, float)) else None,
            ))

    return Stackup(layers=layers)


def from_sidecar(data: Dict[str, Any]) -> DesignData:
    dd = DesignData(source="sidecar")

    stackup = data.get("stackup")
    if isinstance(stackup, dict):
        dd.stackup = _stackup_from_dict(stackup)

    for spec in data.get("controlled_impedance") or []:
        if not isinstance(spec, dict):
            continue
        target = spec.get("target_ohm")
        if not isinstance(target, (int, float)):
            continue
        dd.controlled_impedance.append(ControlledImpedanceSpec(
            name=str(spec.get("name", "?")),
            target_ohm=float(target),
            width_mm=(float(spec["width_mm"]) if isinstance(spec.get("width_mm"), (int, float)) else None),
            tolerance_pct=(float(spec["tolerance_pct"]) if isinstance(spec.get("tolerance_pct"), (int, float)) else 10.0),
        ))

    nets = data.get("nets")
    if isinstance(nets, dict):
        for name, ninfo in nets.items():
            ninfo = ninfo if isinstance(ninfo, dict) else {}
            length = ninfo.get("routed_length_mm", 0.0)
            features = []
            if isinstance(length, (int, float)):
                features.append(NetFeature(layer=None, length_mm=float(length)))
            dd.add_net(Net(
                name=str(name),
                features=features,
                net_class=ninfo.get("net_class"),
            ))

    for dp in data.get("diff_pairs") or []:
        if not isinstance(dp, dict):
            continue
        pos, neg = dp.get("positive"), dp.get("negative")
        if not pos or not neg:
            continue
        dd.diff_pairs.append(DiffPair(
            name=str(dp.get("name", f"{pos}/{neg}")),
            positive=str(pos),
            negative=str(neg),
            target_ohm=(float(dp["target_ohm"]) if isinstance(dp.get("target_ohm"), (int, float)) else None),
        ))

    return dd
