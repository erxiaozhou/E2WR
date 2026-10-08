#!/usr/bin/env python3
"""Generates e2wr-ir's instruction-type static table inst_types_gen.rs from E2WR's inst_data/*.json.

Produces `crates/e2wr-ir/src/inst_types_gen.rs`:
- `concrete_cands(inst: &Inst) -> Option<TR>`: the candidate type sets of instructions
  whose full_type_part entries are all concrete type names (the naive table of Python InstTypeReqF).
  The 5 candidates of (immediate-less) select are included too (the pre-expanded full_type_part form).

Instructions not in the static table (hand-written in types.rs's get_inst_ty_req):
- to_skip_ops: block/loop/if/else/end (handled at the AST layer);
- manual: br/br_table/br_if/return/unreachable/call/call_indirect/ref.null
  (mirroring Python OneInstReqUtil._just_get_type_req_manual);
- placeholder instructions: local.get/set/tee, global.get/set, table.fill/get/grow/set
  (types from context), select_1C~t (typed select, type from the immediate).

inst_name → Inst variant name mapping: the snake name of wasmparser's `Name => visit_snake`
macro table == inst_name with '.'→'_'; special cases listed by hand. Unknown reprs always error out.

Usage: python3 tools/gen_inst_types.py
"""

import json
import re
import sys
from pathlib import Path

E2WR_PKG = Path(__file__).resolve().parents[2] / "E2WR"
INST_DATA = E2WR_PKG / "init_parser_data" / "inst_data"
WASMPARSER_LIB = Path(
    "~/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/"
    "wasmparser-0.240.0/src/lib.rs"
).expanduser()
OUT = Path(__file__).resolve().parents[1] / "crates/e2wr-ir/src/inst_types_gen.rs"

CONCRETE_TYPES = {"i32", "i64", "f32", "f64", "v128", "funcref", "externref"}
TYENUM = {t: f"ValTy::{t.capitalize()}" if t[0].isalpha() and not t[0].isdigit() else None
          for t in CONCRETE_TYPES}
# Fix map casing (V128 not V128.capitalize()=V128 OK; funcref→Funcref; externref→Externref)
TYENUM = {
    "i32": "ValTy::I32", "i64": "ValTy::I64", "f32": "ValTy::F32",
    "f64": "ValTy::F64", "v128": "ValTy::V128", "funcref": "ValTy::Funcref",
    "externref": "ValTy::Externref",
}

TO_SKIP_OPS = {"if", "loop", "block", "else", "end",
               # the immediate-carrying encoded-description form of if; the real decode's opcode_text is 'if'
               # (proven by the S4 driver splitting on opcode_text); unreachable dead data in this file
               "if_04~bt~in_1~05~in_2~0B"}
MANUAL_OPS = {"br", "br_table", "br_if", "return", "unreachable",
              "call_indirect", "call", "ref.null"}
# inst_name → variant-name special cases (not covered by the snake rule)
NAME_SPECIAL = {
    "select_1C~t": "TypedSelect",
}


def load_operator_map():
    """wasmparser macro table → {visit_snake name: Operator variant name}."""
    src = WASMPARSER_LIB.read_text()
    start = src.find("macro_rules! _for_each_operator_group")
    end = src.find("\n}\n", start)
    table = src[start:end]
    pat = re.compile(r"([A-Z][A-Za-z0-9]*)\s*(?:\{[^}]*\})?\s*=>\s*(visit_\w+)")
    m = {}
    for name, visit in pat.findall(table):
        snake = visit[len("visit_"):]
        if snake in m and m[snake] != name:
            sys.exit(f"snake collision: {snake} -> {m[snake]} / {name}")
        m[snake] = name
    return m


def variant_of(inst_name, opmap):
    if inst_name in NAME_SPECIAL:
        return NAME_SPECIAL[inst_name], True
    snake = inst_name.replace(".", "_")
    if snake in opmap:
        return opmap[snake], True
    return None, False


def main() -> None:
    opmap = load_operator_map()
    entries = []          # (variant name, [[param strs],[result strs]]) concrete instructions
    placeholder_insts = []  # the failure list outside placeholder/manual/skip
    unresolved = []

    # Aggregate by inst_name. Python's _get_full_type_data_raw collects same-named dict
    # entries with later-overwrites-earlier (behavior depends on iterdir order, P-22); on name conflicts here the
    # concrete-candidate version wins (= the canonical semantics, matching Python's measured current filesystem order)
    def is_concrete(full):
        return all(
            all(x in CONCRETE_TYPES
                for x in c.get("param", []) + c.get("result", []))
            for c in full)

    by_name = {}
    for p in sorted(INST_DATA.glob("*.json")):
        d = json.loads(p.read_text())
        name = d["inst_name"]
        if name in by_name:
            old_full, old_file = by_name[name]
            if is_concrete(old_full) and not is_concrete(d["full_type_part"]):
                print(f"WARN inst_name collision {name!r}: keep concrete "
                      f"{old_file}, drop {p.name}")
                continue  # keep the old concrete version
            print(f"WARN inst_name collision {name!r}: {old_file} -> {p.name}")
        by_name[name] = (d["full_type_part"], p.name)

    for inst_name, (full, src_file) in sorted(by_name.items()):
        if inst_name in TO_SKIP_OPS or inst_name in MANUAL_OPS:
            continue
        if inst_name in NAME_SPECIAL:
            continue  # typed select is hand-written
        cand_ty = []
        ok = True
        for cand in full:
            params, results = cand.get("param", []), cand.get("result", [])
            if all(x in CONCRETE_TYPES for x in params + results):
                cand_ty.append((params, results))
            else:
                # placeholder instructions: local.get/set/tee, global.get/set, table.* hand-written
                placeholder_insts.append(inst_name)
                ok = False
                break
        if not ok:
            continue
        if not cand_ty:
            placeholder_insts.append(inst_name + " (empty)")
            continue
        var, ok2 = variant_of(inst_name, opmap)
        if not ok2:
            unresolved.append(inst_name)
            continue
        entries.append((var, cand_ty))

    if unresolved:
        sys.exit(f"unresolved inst names (no wasmparser variant): {unresolved}")

    lines = []
    w = lines.append
    w("// @generated by tools/gen_inst_types.py — do not edit by hand.")
    w("// Source: full_type_part of E2WR/init_parser_data/inst_data/*.json")
    w("// (concrete type candidates only; placeholder and manual instructions live in get_inst_ty_req in types.rs).")
    w("#![allow(clippy::all)]")
    w("")
    w("use crate::inst_gen::Inst;")
    w("use crate::types::{FTy, TR, ValTy};")
    w("")
    w("/// Candidate sets of concrete-typed instructions (context-free; None = handled by the manual path or the AST layer).")
    w("pub fn concrete_cands(inst: &Inst) -> Option<TR> {")
    w("    match inst {")
    for var, cands in entries:
        parts = []
        for params, results in cands:
            p = ", ".join(TYENUM[t] for t in params)
            r = ", ".join(TYENUM[t] for t in results)
            parts.append(f"FTy::of(&[{p}], &[{r}])")
        one = parts[0] if len(parts) == 1 else f"{parts[0]} /* see the generator for the remaining candidates */"
        w(f"        Inst::{var} {{ .. }} => Some(TR::new([")
        for pp in parts:
            w(f"            {pp},")
        w("        ])),")
    w("        _ => None,")
    w("    }")
    w("}")
    OUT.write_text("\n".join(lines) + "\n")
    print(f"concrete instructions: {len(entries)}")
    print(f"placeholder (hand-written): {sorted(set(placeholder_insts))}")


if __name__ == "__main__":
    main()
