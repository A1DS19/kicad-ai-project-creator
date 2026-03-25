"""
KiCad Tool Dispatcher

Two backends:
  - kicad-cli  : subprocess calls for all fab outputs and ERC/DRC
                 (IPC API has no export support — kicad-cli is the right path)
  - kipy IPC   : live KiCad connection for PCB read/write operations
                 `pip install kicad-python` — module name is `kipy`
                 Connects via KICAD_API_SOCKET env var or /tmp/kicad/api.sock
                 KiCad must be running. No schematic API yet (PCB only).
  - stubs      : in-memory fallback when no project file is set

Call set_project(pcb_file=..., sch_file=...) before any real operation.
"""

from __future__ import annotations
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Internal project state (replace with real KiCad project object)
# ─────────────────────────────────────────────────────────────────────────────

_project_state: dict[str, Any] = {
    "pcb_file":    None,   # path to .kicad_pcb
    "sch_file":    None,   # path to .kicad_sch
    "sheets": {},          # sheet_name → {components, nets, labels}
    "footprints": {},      # reference → footprint_path
    "placements": {},      # reference → {x, y, rotation, layer}
    "zones": [],           # list of zone dicts
    "traces": [],          # list of trace dicts
    "vias": [],            # list of via dicts
    "board_outline": None, # {width, height, corner_radius}
    "bom": {},             # reference → component info
}


# ─────────────────────────────────────────────────────────────────────────────
# Project setup
# ─────────────────────────────────────────────────────────────────────────────

def set_drc_severity(rule_type: str, severity: str) -> dict:
    """
    Change a rule_severities entry in the .kicad_pro file.
    Edits the JSON in place — safe to call multiple times.
    """
    pcb = _pcb_file()
    if not pcb:
        return {"status": "error", "message": "Call set_project(pcb_file=...) first."}

    pro_file = Path(pcb).with_suffix(".kicad_pro")
    if not pro_file.exists():
        return {"status": "error", "message": f"Project file not found: {pro_file}"}

    try:
        data = json.loads(pro_file.read_text())
        severities = data.setdefault("board", {}).setdefault(
            "design_settings", {}
        ).setdefault("rule_severities", {})

        old = severities.get(rule_type, "not set")
        severities[rule_type] = severity
        pro_file.write_text(json.dumps(data, indent=2))

        return {
            "status": "ok",
            "rule_type": rule_type,
            "old_severity": old,
            "new_severity": severity,
            "file": str(pro_file),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def add_drc_exclusion(
    reference: str,
    rule_types: list[str],
    reason: str = "",
) -> dict:
    """
    Write a custom rule to the .kicad_dru file that ignores specific DRC
    checks for a named footprint reference.

    KiCad loads .kicad_dru automatically when it shares a name with the .kicad_pro.
    Format: https://docs.kicad.org/en/design_rules/design_rules.html
    """
    pcb = _pcb_file()
    if not pcb:
        return {"status": "error", "message": "Call set_project(pcb_file=...) first."}

    dru_file = Path(pcb).with_suffix(".kicad_dru")

    # Build one rule block per rule_type for the given reference
    lines = []
    if reason:
        lines.append(f"# {reason}")
    for rule_type in rule_types:
        rule_name = f"exclude_{rule_type}_{reference}".replace(" ", "_")
        lines.append(f"(rule \"{rule_name}\"")
        lines.append(f"  (severity ignore)")
        lines.append(f"  (condition \"A.Reference == '{reference}' || B.Reference == '{reference}'\")")
        lines.append(f"  (constraint {rule_type})")
        lines.append(f")")
        lines.append("")

    block = "\n".join(lines)

    try:
        existing = dru_file.read_text() if dru_file.exists() else ""
        dru_file.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "") + block)
        return {
            "status": "ok",
            "reference": reference,
            "rule_types": rule_types,
            "dru_file": str(dru_file),
            "note": "Reload the PCB in KiCad (or run DRC again) to apply.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def set_project(
    pcb_file: str | None = None,
    sch_file: str | None = None,
) -> dict:
    """Store the active project file paths for this session."""
    if pcb_file:
        p = Path(pcb_file).expanduser().resolve()
        if not p.exists():
            return {"status": "error", "message": f"PCB file not found: {p}"}
        _project_state["pcb_file"] = str(p)
    if sch_file:
        s = Path(sch_file).expanduser().resolve()
        if not s.exists():
            return {"status": "error", "message": f"Schematic file not found: {s}"}
        _project_state["sch_file"] = str(s)
    return {
        "status": "ok",
        "pcb_file": _project_state["pcb_file"],
        "sch_file": _project_state["sch_file"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# kipy IPC connection (PCB only — no schematic API yet)
# ─────────────────────────────────────────────────────────────────────────────

def _kicad():
    """Return a connected kipy.kicad.KiCad instance, or raise ImportError / ConnectionError."""
    from kipy.kicad import KiCad  # pip install kicad-python
    return KiCad()


def _pcb_file(override: str | None = None) -> str | None:
    return override or _project_state.get("pcb_file")


def _sch_file(override: str | None = None) -> str | None:
    return override or _project_state.get("sch_file")


# ─────────────────────────────────────────────────────────────────────────────
# kicad-cli helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run_cli(*args: str) -> tuple[int, str, str]:
    """Run kicad-cli and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["kicad-cli", *args],
        capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _cli_error(stderr: str, returncode: int) -> dict:
    return {"status": "error", "message": stderr.strip() or f"kicad-cli exited {returncode}"}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 0-1: Research & Validation
# ─────────────────────────────────────────────────────────────────────────────

def search_components(
    query: str,
    package: str | None = None,
    max_results: int = 5,
    in_stock_only: bool = True,
    preferred_distributors: list[str] | None = None,
) -> dict:
    """
    Stub: Search Octopart/Mouser for components.

    Replace with real Octopart GraphQL API call:
      https://octopart.com/api/v4/reference
    or Mouser Search API:
      https://www.mouser.com/api-hub/
    """
    return {
        "status": "ok",
        "note": "STUB — wire up a real distributor API",
        "results": [
            {
                "mpn": f"STUB-{query[:8].upper().replace(' ', '-')}",
                "manufacturer": "StubCorp",
                "description": query,
                "package": package or "SOT-23",
                "stock_mouser": 9999,
                "price_usd_qty10": 0.42,
                "kicad_footprint_hint": f"Package_TO_SOT_SMD:{package or 'SOT-23'}",
                "datasheet_url": "https://example.com/stub_datasheet.pdf",
            }
        ],
    }


def get_datasheet(mpn: str, manufacturer: str | None = None) -> dict:
    """
    Stub: Fetch and parse a component datasheet.

    Replace with a PDF fetch + LLM parsing pipeline, or a structured
    datasheet database (e.g., IHS Markit, octopart datasheet endpoint).
    """
    return {
        "status": "ok",
        "note": "STUB — wire up a real datasheet fetch/parse pipeline",
        "mpn": mpn,
        "manufacturer": manufacturer or "Unknown",
        "pins": [],
        "recommended_footprint": "Package_TO_SOT_SMD:SOT-23",
        "decoupling_recommendation": "100nF ceramic on each VCC pin",
        "layout_notes": "Keep decoupling caps within 2mm of power pins.",
        "max_ratings": {},
    }


def verify_kicad_footprint(library: str, footprint: str) -> dict:
    """
    Stub: Check whether a footprint exists in the KiCad standard libraries.

    Replace with an actual filesystem search of your KiCad footprint library
    paths (e.g. /usr/share/kicad/footprints/) or a pcbnew API call.
    """
    full_path = f"{library}:{footprint}"
    # Pretend common footprints exist so the agent can proceed in demos.
    known_prefixes = (
        "Resistor_SMD", "Capacitor_SMD", "Package_TO_SOT_SMD",
        "Package_SO", "Package_DFN_QFN", "Connector_USB",
        "Connector_JST", "RF_Module",
    )
    found = any(library.startswith(p) for p in known_prefixes)
    return {
        "status": "ok",
        "found": found,
        "full_path": full_path if found else None,
        "close_matches": [] if found else [
            f"{library}:{footprint}_HandSoldering",
            f"{library}:{footprint.split('_')[0]}",
        ],
        "note": "STUB — replace with real KiCad library lookup",
    }


def generate_custom_footprint(
    reference: str,
    package_type: str,
    pad_count: int,
    pitch_mm: float | None = None,
    body_width_mm: float | None = None,
    body_height_mm: float | None = None,
    pad_width_mm: float | None = None,
    pad_height_mm: float | None = None,
    courtyard_margin_mm: float = 0.5,
) -> dict:
    """
    Stub: Generate a .kicad_mod file from land-pattern dimensions.

    Replace with a proper KiCad footprint generator, e.g.:
      - KiCad's IPC-7351 footprint wizard
      - kicad-footprint-generator (github.com/KiCad/kicad-footprint-generator)
    """
    fp_name = f"Custom_{reference}_{package_type}_{pad_count}pad"
    return {
        "status": "ok",
        "note": "STUB — replace with real footprint generator",
        "footprint_name": fp_name,
        "library_path": f"[project]:{fp_name}",
        "kicad_mod_written": False,
    }


def impedance_calc(
    target_impedance_ohm: float,
    trace_type: str,
    layer: str = "F.Cu",
    dielectric_thickness_mm: float = 0.2,
    dielectric_constant: float = 4.5,
    copper_thickness_mm: float = 0.035,
) -> dict:
    """
    Impedance calculator using simplified closed-form IPC-2141A formulae.
    Good to ±10% for standard FR4 stackups — use a proper field solver for
    production designs (e.g. Saturn PCB toolkit, Polar Si9000).

    Microstrip: Z0 = (87 / sqrt(Er + 1.41)) * ln(5.98*H / (0.8*W + T))
    where H = dielectric_thickness, W = trace_width, T = copper_thickness.
    We solve for W iteratively.
    """
    if trace_type == "microstrip":
        H = dielectric_thickness_mm
        T = copper_thickness_mm
        Er = dielectric_constant
        # Solve W from target Z0 numerically (bisection)
        def z0(W):
            return (87.0 / math.sqrt(Er + 1.41)) * math.log(5.98 * H / (0.8 * W + T))
        lo, hi = 0.01, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if z0(mid) > target_impedance_ohm:
                lo = mid
            else:
                hi = mid
        width_mm = round((lo + hi) / 2.0, 4)
        return {
            "status": "ok",
            "trace_type": trace_type,
            "target_impedance_ohm": target_impedance_ohm,
            "calculated_width_mm": width_mm,
            "layer": layer,
            "stackup": {
                "dielectric_thickness_mm": H,
                "dielectric_constant": Er,
                "copper_thickness_mm": T,
            },
            "note": "IPC-2141A microstrip approximation ±10%. Verify with field solver for production.",
        }

    if trace_type == "differential":
        # Approximate: each trace ~= single-ended Z0 at half spacing
        # Use the microstrip formula as a starting point, then derate for coupling
        single = impedance_calc(
            target_impedance_ohm * 0.55,
            "microstrip", layer,
            dielectric_thickness_mm, dielectric_constant, copper_thickness_mm,
        )
        w = single["calculated_width_mm"]
        s = round(w * 1.5, 4)   # recommended spacing = 1.5× width
        return {
            "status": "ok",
            "trace_type": "differential",
            "target_differential_impedance_ohm": target_impedance_ohm,
            "calculated_width_mm": w,
            "recommended_spacing_mm": s,
            "note": (
                "Differential pair estimate — each trace width approximated. "
                "Verify with a proper differential pair impedance calculator."
            ),
        }

    return {
        "status": "error",
        "message": (
            f"Impedance calculation for trace_type='{trace_type}' not implemented. "
            "Use a field solver for stripline / coplanar waveguide."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2: Schematic
# ─────────────────────────────────────────────────────────────────────────────

def create_schematic_sheet(
    sheet_name: str,
    sheet_number: int,
    title: str,
    revision: str = "v1.0",
) -> dict:
    _project_state["sheets"][sheet_name] = {
        "number": sheet_number,
        "title": title,
        "revision": revision,
        "symbols": [],
        "wires": [],
        "labels": [],
        "no_connects": [],
    }
    return {"status": "ok", "sheet_name": sheet_name, "sheet_number": sheet_number}


def add_symbol(
    library: str,
    symbol: str,
    reference: str,
    value: str,
    x: float,
    y: float,
    sheet: str,
    rotation: float = 0,
    mirror_x: bool = False,
) -> dict:
    if sheet not in _project_state["sheets"]:
        return {"status": "error", "message": f"Sheet '{sheet}' not found. Create it first."}
    entry = {
        "library": library, "symbol": symbol,
        "reference": reference, "value": value,
        "x": x, "y": y, "rotation": rotation, "mirror_x": mirror_x,
    }
    _project_state["sheets"][sheet]["symbols"].append(entry)
    _project_state["bom"][reference] = {"value": value, "library": library, "symbol": symbol}
    return {"status": "ok", "reference": reference, "sheet": sheet}


def add_power_symbol(net_name: str, x: float, y: float, sheet: str) -> dict:
    if sheet not in _project_state["sheets"]:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    _project_state["sheets"][sheet]["symbols"].append({
        "library": "power", "symbol": net_name,
        "reference": f"#{net_name}", "value": net_name,
        "x": x, "y": y, "rotation": 0, "mirror_x": False,
    })
    return {"status": "ok", "net_name": net_name, "sheet": sheet}


def connect_pins(
    from_ref: str, from_pin: str,
    to_ref: str, to_pin: str,
    sheet: str,
) -> dict:
    if sheet not in _project_state["sheets"]:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    _project_state["sheets"][sheet]["wires"].append({
        "from_ref": from_ref, "from_pin": from_pin,
        "to_ref": to_ref, "to_pin": to_pin,
    })
    return {"status": "ok"}


def add_net_label(
    net_name: str,
    sheet: str,
    snap_to_ref: str | None = None,
    snap_to_pin: str | None = None,
    x: float | None = None,
    y: float | None = None,
    rotation: float = 0,
) -> dict:
    if sheet not in _project_state["sheets"]:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}

    # Snap-to-pin: resolve coordinates from the symbol's pin endpoint.
    # In a real implementation this calls get_pin_positions() and looks up the pin.
    # The stub records the intent so Claude can see the snap was requested.
    if snap_to_ref and snap_to_pin:
        label = {"net_name": net_name, "snap_to_ref": snap_to_ref,
                 "snap_to_pin": snap_to_pin, "rotation": rotation,
                 "note": "STUB — real impl resolves pin endpoint coords automatically"}
        _project_state["sheets"][sheet]["labels"].append(label)
        return {
            "status": "ok", "net_name": net_name,
            "snapped_to": f"{snap_to_ref}:{snap_to_pin}",
            "note": "STUB — pin endpoint will be resolved in real implementation",
        }

    if x is None or y is None:
        return {"status": "error", "message": "Provide either snap_to_ref+snap_to_pin or explicit x,y."}

    _project_state["sheets"][sheet]["labels"].append(
        {"net_name": net_name, "x": x, "y": y, "rotation": rotation}
    )
    return {"status": "ok", "net_name": net_name, "x": x, "y": y}


def add_no_connect(reference: str, pin: str, sheet: str) -> dict:
    if sheet not in _project_state["sheets"]:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    _project_state["sheets"][sheet]["no_connects"].append(
        {"reference": reference, "pin": pin}
    )
    return {"status": "ok"}


def remove_no_connect(reference: str, pin: str, sheet: str) -> dict:
    if sheet not in _project_state["sheets"]:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    nc_list = _project_state["sheets"][sheet]["no_connects"]
    before = len(nc_list)
    _project_state["sheets"][sheet]["no_connects"] = [
        nc for nc in nc_list
        if not (nc["reference"] == reference and nc["pin"] == pin)
    ]
    removed = before - len(_project_state["sheets"][sheet]["no_connects"])
    if removed == 0:
        return {"status": "error", "message": f"No no-connect marker found for {reference}:{pin}."}
    return {"status": "ok", "removed": removed, "reference": reference, "pin": pin}


def get_pin_positions(reference: str, sheet: str) -> dict:
    """
    Return all pin endpoints in schematic coordinates.

    Real implementation must:
      1. Locate the symbol's (lib_id) definition inside (lib_symbols ...) in the .kicad_sch
      2. For each pin: read its (at x y angle) in symbol space
      3. Apply placement transform: rotate by symbol rotation, flip if mirror_x=True
      4. Apply Y-inversion: sch_y = place_y - sym_pin_y  (KiCad flips Y between spaces)
      5. Add placement offset: sch_x = place_x + rotated_pin_x

    All returned coordinates are in schematic space — callers never see symbol space.
    """
    sheet_data = _project_state["sheets"].get(sheet)
    if not sheet_data:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    symbol = next(
        (s for s in sheet_data["symbols"] if s["reference"] == reference), None
    )
    if not symbol:
        return {"status": "error", "message": f"Symbol '{reference}' not found on sheet '{sheet}'."}
    return {
        "status": "ok",
        "note": "STUB — replace with real pin endpoint calculation (see docstring for transform steps)",
        "reference": reference,
        "placement": {"x": symbol["x"], "y": symbol["y"], "rotation": symbol.get("rotation", 0)},
        "coordinate_space": "schematic (Y-inversion applied)",
        "pins": [],  # Real: [{pin_name, pin_number, sch_x, sch_y, direction}]
    }


def move_symbol(
    reference: str,
    x: float,
    y: float,
    sheet: str,
    rotation: float | None = None,
) -> dict:
    sheet_data = _project_state["sheets"].get(sheet)
    if not sheet_data:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    symbol = next(
        (s for s in sheet_data["symbols"] if s["reference"] == reference), None
    )
    if not symbol:
        return {"status": "error", "message": f"Symbol '{reference}' not found on sheet '{sheet}'."}
    old = {"x": symbol["x"], "y": symbol["y"], "rotation": symbol.get("rotation", 0)}
    symbol["x"] = x
    symbol["y"] = y
    if rotation is not None:
        symbol["rotation"] = rotation
    return {"status": "ok", "reference": reference, "from": old, "to": {"x": x, "y": y}}


def move_label(
    net_name: str,
    sheet: str,
    snap_to_ref: str | None = None,
    snap_to_pin: str | None = None,
    x: float | None = None,
    y: float | None = None,
    rotation: float | None = None,
) -> dict:
    sheet_data = _project_state["sheets"].get(sheet)
    if not sheet_data:
        return {"status": "error", "message": f"Sheet '{sheet}' not found."}
    label = next(
        (lb for lb in sheet_data["labels"] if lb["net_name"] == net_name), None
    )
    if not label:
        return {"status": "error", "message": f"Label '{net_name}' not found on sheet '{sheet}'."}

    if snap_to_ref and snap_to_pin:
        label["snap_to_ref"] = snap_to_ref
        label["snap_to_pin"] = snap_to_pin
        if rotation is not None:
            label["rotation"] = rotation
        return {
            "status": "ok", "net_name": net_name,
            "snapped_to": f"{snap_to_ref}:{snap_to_pin}",
            "note": "STUB — pin endpoint will be resolved in real implementation",
        }

    if x is None or y is None:
        return {"status": "error", "message": "Provide either snap_to_ref+snap_to_pin or explicit x,y."}

    label["x"] = x
    label["y"] = y
    if rotation is not None:
        label["rotation"] = rotation
    return {"status": "ok", "net_name": net_name, "x": x, "y": y}


def assign_footprint(reference: str, footprint_path: str) -> dict:
    _project_state["footprints"][reference] = footprint_path
    if reference in _project_state["bom"]:
        _project_state["bom"][reference]["footprint"] = footprint_path
    return {"status": "ok", "reference": reference, "footprint": footprint_path}


def run_erc(scope: str = "all") -> dict:
    """
    Run ERC via kicad-cli. Returns structured violations with suggested fixes.
    Requires set_project(sch_file=...) to have been called first.
    """
    sch = _sch_file()
    if not sch:
        return _stub_erc(scope)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = f.name

    rc, stdout, stderr = _run_cli(
        "sch", "erc",
        "--format", "json",
        "--severity-all",
        "--output", out,
        sch,
    )

    try:
        raw = json.loads(Path(out).read_text())
    except Exception:
        return _cli_error(stderr, rc)
    finally:
        Path(out).unlink(missing_ok=True)

    errors, warnings = [], []
    for v in raw.get("violations", []):
        sev = v.get("severity", "error").lower()
        items = v.get("items", [])
        ref = items[0].get("description", "") if items else ""
        pos = items[0].get("pos", {}) if items else {}
        entry = {
            "type": v.get("type", "unknown"),
            "severity": sev,
            "symbol_ref": ref,
            "pin_name": items[1].get("description", "") if len(items) > 1 else None,
            "position_x": pos.get("x"),
            "position_y": pos.get("y"),
            "description": v.get("description", ""),
            "suggested_fix": _erc_suggested_fix(v),
        }
        (errors if sev == "error" else warnings).append(entry)

    return {
        "status": "ok",
        "source": "kicad-cli",
        "sch_file": sch,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def _erc_suggested_fix(v: dict) -> str:
    t = v.get("type", "")
    desc = v.get("description", "")
    if "dangling" in t or "dangling" in desc.lower():
        return "Use snap_label_to_pin or move_label with snap_to_ref+snap_to_pin to connect the label to the nearest pin endpoint."
    if "unconnected" in t:
        return "Add a net label, wire, or no-connect marker to this pin."
    if "power_flag" in t or "PWR_FLAG" in desc:
        return "Add a PWR_FLAG symbol to this power net."
    if "duplicate" in t:
        return "Renumber the duplicate reference designator."
    return desc


def _stub_erc(scope: str) -> dict:
    """In-memory stub ERC used when no sch_file is set."""
    errors, warnings = [], []
    for ref in _project_state["bom"]:
        if ref not in _project_state["footprints"]:
            warnings.append({
                "type": "missing_footprint", "severity": "warning",
                "symbol_ref": ref, "pin_name": None,
                "position_x": None, "position_y": None,
                "suggested_fix": f"Call assign_footprint(reference='{ref}', footprint_path='...')",
            })
    for sheet_data in _project_state["sheets"].values():
        for label in sheet_data.get("labels", []):
            if "snap_to_ref" in label and "x" not in label:
                errors.append({
                    "type": "label_dangling", "severity": "error",
                    "symbol_ref": label.get("snap_to_ref"),
                    "pin_name": label.get("snap_to_pin"),
                    "position_x": None, "position_y": None,
                    "suggested_fix": (
                        f"Resolve pin endpoint for {label['snap_to_ref']}:{label['snap_to_pin']} "
                        f"and snap label '{label['net_name']}' there."
                    ),
                })
    return {
        "status": "ok", "source": "stub",
        "note": "Set sch_file via set_project() to run real ERC",
        "error_count": len(errors), "warning_count": len(warnings),
        "errors": errors, "warnings": warnings,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4: PCB Layout
# ─────────────────────────────────────────────────────────────────────────────

def set_board_outline(
    width_mm: float,
    height_mm: float,
    corner_radius_mm: float = 1.0,
    origin_x_mm: float = 0,
    origin_y_mm: float = 0,
) -> dict:
    _project_state["board_outline"] = {
        "width": width_mm, "height": height_mm,
        "corner_radius": corner_radius_mm,
        "origin_x": origin_x_mm, "origin_y": origin_y_mm,
    }
    return {
        "status": "ok",
        "board_area_mm2": round(width_mm * height_mm, 2),
        "outline": _project_state["board_outline"],
    }


def add_mounting_holes(
    drill_mm: float = 3.2,
    pad_mm: float = 6.0,
    positions: str = "corners",
    corner_offset_mm: float = 3.5,
) -> dict:
    outline = _project_state.get("board_outline")
    if not outline:
        return {"status": "error", "message": "Set board outline before adding mounting holes."}
    w, h = outline["width"], outline["height"]
    d = corner_offset_mm
    hole_positions = [
        {"ref": "H1", "x": d,   "y": d},
        {"ref": "H2", "x": w-d, "y": d},
        {"ref": "H3", "x": w-d, "y": h-d},
        {"ref": "H4", "x": d,   "y": h-d},
    ]
    for hp in hole_positions:
        _project_state["placements"][hp["ref"]] = {
            "x": hp["x"], "y": hp["y"],
            "rotation": 0, "layer": "F.Cu",
            "drill_mm": drill_mm, "pad_mm": pad_mm,
        }
    return {"status": "ok", "holes_added": 4, "positions": hole_positions}


def place_footprint(
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation_deg: float = 0,
    layer: str = "F.Cu",
) -> dict:
    """
    Move a footprint to the given position via kipy IPC.
    KiCad must be running with the PCB open. Falls back to in-memory stub if not.
    """
    try:
        from kipy.kicad import KiCad
        from kipy.geometry import Vector2, Angle
        from kipy.board_types import BoardLayer

        kicad = KiCad()
        board = kicad.get_board()

        # Find footprint by reference designator
        fps = board.get_footprints()
        fp = next(
            (f for f in fps if f.reference_field.text.value == reference),
            None,
        )
        if fp is None:
            return {"status": "error", "message": f"Footprint '{reference}' not found on board."}

        old_pos = fp.position
        fp.position = Vector2.from_xy_mm(x_mm, y_mm)
        fp.orientation = Angle.from_degrees(rotation_deg)
        fp.layer = BoardLayer.BL_B_Cu if layer == "B.Cu" else BoardLayer.BL_F_Cu

        board.update_items(fp)
        board.save()

        return {
            "status": "ok",
            "source": "kipy",
            "reference": reference,
            "x_mm": x_mm,
            "y_mm": y_mm,
            "rotation_deg": rotation_deg,
            "layer": layer,
            "from": {
                "x_mm": old_pos.x / 1_000_000,
                "y_mm": old_pos.y / 1_000_000,
            },
        }

    except ImportError:
        pass
    except Exception as e:
        if "connect" in str(e).lower() or "socket" in str(e).lower():
            return {
                "status": "error",
                "message": "KiCad is not running. Open the PCB in KiCad then retry.",
            }
        return {"status": "error", "message": f"kipy error: {e}"}

    # Stub fallback — no BOM validation, real PCB is the source of truth
    _project_state["placements"][reference] = {
        "x": x_mm, "y": y_mm, "rotation": rotation_deg, "layer": layer,
    }
    return {
        "status": "ok",
        "source": "stub",
        "note": "KiCad not running — open PCB in KiCad for live placement",
        "reference": reference,
        "x_mm": x_mm,
        "y_mm": y_mm,
    }


def get_ratsnest(net_filter: str | None = None) -> dict:
    """
    Return nets and unconnected count via kipy IPC.
    Falls back to stub if KiCad is not running.
    """
    try:
        from kipy.kicad import KiCad

        kicad = KiCad()
        board = kicad.get_board()
        nets = board.get_nets()

        net_list = [
            {"name": n.name, "net_code": n.net_code}
            for n in nets
            if not net_filter or net_filter.lower() in n.name.lower()
        ]

        # Unconnected count comes from DRC — use kicad-cli if pcb_file is set
        unconnected_count = None
        pcb = _pcb_file()
        if pcb:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
                out = f.name
            rc, _, _ = _run_cli("pcb", "drc", "--format", "json",
                                 "--severity-error", "--output", out, pcb)
            try:
                raw = json.loads(Path(out).read_text())
                unconnected_count = len(raw.get("unconnected_items", []))
            except Exception:
                pass
            finally:
                Path(out).unlink(missing_ok=True)

        return {
            "status": "ok",
            "source": "kipy",
            "net_count": len(net_list),
            "unconnected_count": unconnected_count,
            "nets": net_list,
        }

    except ImportError:
        pass
    except Exception as e:
        if "connect" in str(e).lower() or "socket" in str(e).lower():
            return {
                "status": "error",
                "message": "KiCad is not running. Open the PCB in KiCad then retry.",
            }
        return {"status": "error", "message": f"kipy error: {e}"}

    # Stub fallback
    placed = set(_project_state["placements"].keys())
    unplaced = set(_project_state["bom"].keys()) - placed
    return {
        "status": "ok",
        "source": "stub",
        "note": "KiCad not running — open PCB in KiCad for live ratsnest",
        "unconnected_count": None,
        "unplaced_components": list(unplaced),
        "nets": [],
    }


def add_keepout_zone(
    outline_mm: list[list[float]],
    no_copper: bool = True,
    no_vias: bool = True,
    no_footprints: bool = False,
    reason: str = "",
) -> dict:
    _project_state["zones"].append({
        "type": "keepout",
        "outline_mm": outline_mm,
        "no_copper": no_copper,
        "no_vias": no_vias,
        "no_footprints": no_footprints,
        "reason": reason,
    })
    return {"status": "ok", "reason": reason}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5: Copper Pours
# ─────────────────────────────────────────────────────────────────────────────

def add_zone(
    net_name: str,
    layer: str,
    outline_mm: list[list[float]],
    clearance_mm: float = 0.3,
    min_width_mm: float = 0.25,
    fill_mode: str = "solid",
    priority: int = 0,
) -> dict:
    _project_state["zones"].append({
        "type": "copper",
        "net_name": net_name,
        "layer": layer,
        "outline_mm": outline_mm,
        "clearance_mm": clearance_mm,
        "min_width_mm": min_width_mm,
        "fill_mode": fill_mode,
        "priority": priority,
        "filled": False,
    })
    return {"status": "ok", "net_name": net_name, "layer": layer}


def fill_zones() -> dict:
    """
    Refill all copper zones via kipy IPC.
    KiCad must be running with the PCB open. Falls back to stub if not.
    """
    try:
        from kipy.kicad import KiCad

        kicad = KiCad()
        board = kicad.get_board()
        board.refill_zones()
        board.save()

        zone_count = len(board.get_zones())
        return {"status": "ok", "source": "kipy", "zones_filled": zone_count}

    except ImportError:
        pass
    except Exception as e:
        if "connect" in str(e).lower() or "socket" in str(e).lower():
            return {
                "status": "error",
                "message": "KiCad is not running. Open the PCB in KiCad then retry.",
            }
        return {"status": "error", "message": f"kipy error: {e}"}

    # Stub fallback
    filled = sum(1 for z in _project_state["zones"] if z.get("type") == "copper")
    for z in _project_state["zones"]:
        if z.get("type") == "copper":
            z["filled"] = True
    return {"status": "ok", "source": "stub",
            "note": "KiCad not running — zone fill recorded in memory only",
            "zones_filled": filled}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6: Routing
# ─────────────────────────────────────────────────────────────────────────────

def route_trace(
    net_name: str,
    from_pad: str,
    to_pad: str,
    width_mm: float,
    layer: str,
    via_at: list[float] | None = None,
) -> dict:
    trace = {
        "net_name": net_name, "from_pad": from_pad,
        "to_pad": to_pad, "width_mm": width_mm,
        "layer": layer, "via_at": via_at,
    }
    _project_state["traces"].append(trace)
    return {"status": "ok", "net_name": net_name, "from": from_pad, "to": to_pad}


def route_differential_pair(
    net_positive: str,
    net_negative: str,
    from_ref: str,
    to_ref: str,
    width_mm: float,
    spacing_mm: float,
    layer: str = "F.Cu",
    max_skew_mm: float = 0.1,
) -> dict:
    for net in (net_positive, net_negative):
        _project_state["traces"].append({
            "net_name": net, "from_pad": from_ref,
            "to_pad": to_ref, "width_mm": width_mm,
            "layer": layer, "differential": True,
            "spacing_mm": spacing_mm,
        })
    return {
        "status": "ok",
        "net_positive": net_positive, "net_negative": net_negative,
        "skew_mm": 0.0, "max_skew_mm": max_skew_mm,
    }


def add_via(
    net_name: str,
    x_mm: float,
    y_mm: float,
    drill_mm: float = 0.4,
    pad_mm: float = 0.8,
    from_layer: str = "F.Cu",
    to_layer: str = "B.Cu",
) -> dict:
    _project_state["vias"].append({
        "net_name": net_name, "x": x_mm, "y": y_mm,
        "drill_mm": drill_mm, "pad_mm": pad_mm,
        "from_layer": from_layer, "to_layer": to_layer,
    })
    return {"status": "ok", "net_name": net_name, "x": x_mm, "y": y_mm}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7: Validation
# ─────────────────────────────────────────────────────────────────────────────

def run_drc(rules_preset: str = "default") -> dict:
    """
    Run DRC via kicad-cli. Returns structured violations.
    Requires set_project(pcb_file=...) to have been called first.
    """
    pcb = _pcb_file()
    if not pcb:
        return _stub_drc()

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = f.name

    rc, stdout, stderr = _run_cli(
        "pcb", "drc",
        "--format", "json",
        "--severity-all",
        "--schematic-parity",
        "--output", out,
        pcb,
    )

    try:
        raw = json.loads(Path(out).read_text())
    except Exception:
        return _cli_error(stderr, rc)
    finally:
        Path(out).unlink(missing_ok=True)

    errors, warnings = [], []
    for v in raw.get("violations", []):
        sev = v.get("severity", "error").lower()
        items = v.get("items", [])
        pos = items[0].get("pos", {}) if items else {}
        entry = {
            "type": v.get("type", "unknown"),
            "severity": sev,
            "description": v.get("description", ""),
            "position_x": pos.get("x"),
            "position_y": pos.get("y"),
            "items": [i.get("description", "") for i in items],
        }
        (errors if sev == "error" else warnings).append(entry)

    unconnected = raw.get("unconnected_items", [])

    return {
        "status": "ok",
        "source": "kicad-cli",
        "pcb_file": pcb,
        "rules_preset": rules_preset,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "unconnected_count": len(unconnected),
        "errors": errors,
        "warnings": warnings,
        "unconnected": unconnected,
        "all_clear": len(errors) == 0 and len(unconnected) == 0,
    }


def _stub_drc() -> dict:
    errors = []
    if not _project_state.get("board_outline"):
        errors.append({"type": "missing_outline", "severity": "error",
                       "description": "No board outline defined."})
    unplaced = set(_project_state["bom"].keys()) - set(_project_state["placements"].keys())
    for ref in unplaced:
        errors.append({"type": "unplaced_component", "severity": "error",
                       "description": f"{ref} has not been placed on the PCB."})
    return {
        "status": "ok", "source": "stub",
        "note": "Set pcb_file via set_project() to run real DRC",
        "error_count": len(errors), "warning_count": 0,
        "errors": errors, "all_clear": len(errors) == 0,
    }


def add_silkscreen_text(
    text: str,
    x_mm: float,
    y_mm: float,
    size_mm: float = 1.0,
    layer: str = "F.SilkS",
) -> dict:
    return {"status": "ok", "text": text, "x": x_mm, "y": y_mm, "layer": layer}


def add_test_point(
    net_name: str,
    x_mm: float,
    y_mm: float,
    layer: str = "F.Cu",
    pad_size_mm: float = 1.5,
) -> dict:
    _project_state["placements"][f"TP_{net_name}"] = {
        "x": x_mm, "y": y_mm, "rotation": 0, "layer": layer,
        "type": "test_point", "net": net_name,
    }
    return {"status": "ok", "net_name": net_name, "x": x_mm, "y": y_mm}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 8: Fabrication Outputs
# ─────────────────────────────────────────────────────────────────────────────

def generate_gerbers(
    output_dir: str = "./gerbers",
    layer_count: int | None = None,
    format: str = "gerber_x2",
) -> dict:
    """Generate Gerber files via kicad-cli. Requires set_project(pcb_file=...)."""
    pcb = _pcb_file()
    if not pcb:
        return {"status": "error", "message": "Call set_project(pcb_file=...) first."}

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    layers = "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,F.Paste,B.Paste,Edge.Cuts,F.Fab,B.Fab,F.Courtyard,B.Courtyard"
    if layer_count and layer_count >= 4:
        layers = "F.Cu,In1.Cu,In2.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,F.Paste,B.Paste,Edge.Cuts,F.Fab,B.Fab"

    args = ["pcb", "export", "gerbers", "--output", output_dir, "--layers", layers]
    if format == "gerber_x1":
        args.append("--no-x2")
    args.append(pcb)

    rc, stdout, stderr = _run_cli(*args)
    if rc != 0:
        return _cli_error(stderr, rc)

    files = [f.name for f in Path(output_dir).iterdir() if f.suffix in (".gbr", ".gtl", ".gbl")]
    return {
        "status": "ok",
        "source": "kicad-cli",
        "output_dir": str(Path(output_dir).resolve()),
        "files_written": len(files),
        "files": sorted(files),
    }


def generate_drill_files(
    output_dir: str = "./gerbers",
    format: str = "excellon",
    merge_pth_npth: bool = False,
) -> dict:
    """Generate drill files via kicad-cli. Requires set_project(pcb_file=...)."""
    pcb = _pcb_file()
    if not pcb:
        return {"status": "error", "message": "Call set_project(pcb_file=...) first."}

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    args = ["pcb", "export", "drill", "--output", output_dir, "--format", format]
    if not merge_pth_npth:
        args.append("--excellon-separate-th")
    args.append(pcb)

    rc, stdout, stderr = _run_cli(*args)
    if rc != 0:
        return _cli_error(stderr, rc)

    files = [f.name for f in Path(output_dir).iterdir() if f.suffix in (".drl", ".xln")]
    return {
        "status": "ok",
        "source": "kicad-cli",
        "output_dir": str(Path(output_dir).resolve()),
        "files_written": len(files),
        "files": sorted(files),
    }


def generate_bom(
    output_path: str | None = None,
    include_prices: bool = True,
    quantity_for_price: int = 10,
    distributors: list[str] | None = None,
) -> dict:
    """Generate BOM via kicad-cli sch export bom. Requires set_project(sch_file=...)."""
    sch = _sch_file()
    if not sch:
        return {"status": "error", "message": "Call set_project(sch_file=...) first."}

    out = output_path or str(Path(sch).parent / "bom.csv")

    rc, stdout, stderr = _run_cli(
        "sch", "export", "bom",
        "--output", out,
        "--fields", "Reference,Value,Footprint,${QUANTITY},Manufacturer,MPN,${DNP}",
        "--labels", "Refs,Value,Footprint,Qty,Manufacturer,MPN,DNP",
        "--group-by", "Value,Footprint",
        "--sort-field", "Reference",
        "--exclude-dnp",
        sch,
    )
    if rc != 0:
        return _cli_error(stderr, rc)

    return {
        "status": "ok",
        "source": "kicad-cli",
        "output_path": out,
        "note": "Pricing/MPN data must be filled in the schematic fields or via a distributor API.",
    }


def generate_position_file(
    output_path: str | None = None,
    units: str = "mm",
    side: str = "both",
) -> dict:
    """Generate pick-and-place position file via kicad-cli. Requires set_project(pcb_file=...)."""
    pcb = _pcb_file()
    if not pcb:
        return {"status": "error", "message": "Call set_project(pcb_file=...) first."}

    out = output_path or str(Path(pcb).parent / "positions.csv")

    rc, stdout, stderr = _run_cli(
        "pcb", "export", "pos",
        "--output", out,
        "--format", "csv",
        "--units", units,
        "--side", side,
        "--exclude-dnp",
        pcb,
    )
    if rc != 0:
        return _cli_error(stderr, rc)

    return {
        "status": "ok",
        "source": "kicad-cli",
        "output_path": out,
        "units": units,
        "side": side,
    }


def generate_3d_model(
    output_path: str | None = None,
    format: str = "step",
) -> dict:
    """Export 3D model via kicad-cli. Requires set_project(pcb_file=...)."""
    pcb = _pcb_file()
    if not pcb:
        return {"status": "error", "message": "Call set_project(pcb_file=...) first."}

    out = output_path or str(Path(pcb).with_suffix(f".{format}"))

    subcommand = "vrml" if format == "wrl" else "step"
    rc, stdout, stderr = _run_cli(
        "pcb", "export", subcommand,
        "--output", out,
        "--force",
        pcb,
    )
    if rc != 0:
        return _cli_error(stderr, rc)

    return {
        "status": "ok",
        "source": "kicad-cli",
        "output_path": out,
        "format": format,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FILESYSTEM
# ─────────────────────────────────────────────────────────────────────────────

def list_directory(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"status": "error", "message": f"Path not found: {path}"}
    if not p.is_dir():
        return {"status": "error", "message": f"Not a directory: {path}"}
    entries = []
    for item in sorted(p.iterdir()):
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size_bytes": item.stat().st_size if item.is_file() else None,
        })
    return {"status": "ok", "path": str(p.resolve()), "entries": entries}


def read_file(path: str, max_bytes: int = 65536) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"status": "error", "message": f"File not found: {path}"}
    if not p.is_file():
        return {"status": "error", "message": f"Not a file: {path}"}
    size = p.stat().st_size
    try:
        raw = p.read_bytes()[:max_bytes]
        content = raw.decode("utf-8", errors="replace")
        return {
            "status": "ok",
            "path": str(p.resolve()),
            "size_bytes": size,
            "truncated": size > max_bytes,
            "content": content,
        }
    except Exception as exc:
        return {"status": "error", "message": f"Could not read file: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# Public dispatcher
# ─────────────────────────────────────────────────────────────────────────────

_DISPATCH_TABLE: dict[str, Any] = {
    # Project setup & DRC rules
    "set_project":             set_project,
    "set_drc_severity":        set_drc_severity,
    "add_drc_exclusion":       add_drc_exclusion,
    # Filesystem
    "list_directory":          list_directory,
    "read_file":               read_file,
    # Phase 0-1
    "search_components":       search_components,
    "get_datasheet":           get_datasheet,
    "verify_kicad_footprint":  verify_kicad_footprint,
    "generate_custom_footprint": generate_custom_footprint,
    "impedance_calc":          impedance_calc,
    # Phase 2
    "create_schematic_sheet":  create_schematic_sheet,
    "add_symbol":              add_symbol,
    "add_power_symbol":        add_power_symbol,
    "connect_pins":            connect_pins,
    "add_net_label":           add_net_label,
    "add_no_connect":          add_no_connect,
    "remove_no_connect":       remove_no_connect,
    "get_pin_positions":       get_pin_positions,
    "move_symbol":             move_symbol,
    "move_label":              move_label,
    "assign_footprint":        assign_footprint,
    "run_erc":                 run_erc,
    # Phase 4
    "set_board_outline":       set_board_outline,
    "add_mounting_holes":      add_mounting_holes,
    "place_footprint":         place_footprint,
    "get_ratsnest":            get_ratsnest,
    "add_keepout_zone":        add_keepout_zone,
    # Phase 5
    "add_zone":                add_zone,
    "fill_zones":              fill_zones,
    # Phase 6
    "route_trace":             route_trace,
    "route_differential_pair": route_differential_pair,
    "add_via":                 add_via,
    # Phase 7
    "run_drc":                 run_drc,
    "add_silkscreen_text":     add_silkscreen_text,
    "add_test_point":          add_test_point,
    # Phase 8
    "generate_gerbers":        generate_gerbers,
    "generate_drill_files":    generate_drill_files,
    "generate_bom":            generate_bom,
    "generate_position_file":  generate_position_file,
    "generate_3d_model":       generate_3d_model,
}


def dispatch_tool(tool_name: str, tool_input: dict) -> dict:
    """
    Route a tool call from the agent to the correct implementation function.

    Parameters
    ----------
    tool_name  : str   — the tool's ``name`` field from the TOOLS list
    tool_input : dict  — the validated input dict from the agent's tool-use block

    Returns
    -------
    dict — JSON-serialisable result forwarded back to the agent as tool_result
    """
    fn = _DISPATCH_TABLE.get(tool_name)
    if fn is None:
        return {
            "status": "error",
            "message": f"Unknown tool '{tool_name}'. Check TOOLS list.",
        }
    try:
        return fn(**tool_input)
    except TypeError as exc:
        return {
            "status": "error",
            "message": f"Tool '{tool_name}' called with invalid arguments: {exc}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "message": f"Tool '{tool_name}' raised an exception: {type(exc).__name__}: {exc}",
        }


def get_project_state() -> dict:
    """Return a snapshot of the current in-memory project state (for debugging)."""
    return _project_state
