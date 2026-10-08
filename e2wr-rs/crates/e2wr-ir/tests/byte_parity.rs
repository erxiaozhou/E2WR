//! O-6 dual-path parity test: the byte pass-through implementation (apply_mutations) and the
//! reference baseline (apply_mutations_reference, clone-and-reencode) must produce byte-identical
//! outputs for the same input and mutations (the design-notes gate for replacement implementations).
//!
//! Two input classes:
//! - synthetic modules (built via from_module, bytes in canonical encoding) covering systematic mutation cases;
//! - real corpus files (from_path, original bytes possibly with non-canonical LEB encodings) — pass-through
//!   keeps original bytes while re-encoding canonicalizes; any difference surfaces here.

#![allow(clippy::field_reassign_with_default)]

use std::path::PathBuf;

use e2wr_ir::module::*;
use wasmparser::ValType;
use e2wr_ir::mutation::{
    apply_mutations, apply_mutations_reference, data_def, export_def, func_def, u32_def,
    DefEdit, Mutation,
};
use e2wr_ir::snapshot::{SectionKind, Snapshot};
use e2wr_ir::{Inst, SectionKind as SK};

fn ty(params: &[ValType], results: &[ValType]) -> FuncType {
    FuncType { params: params.to_vec(), results: results.to_vec() }
}

fn func(ty_idx: u32, insts: Vec<Inst>) -> Func {
    let mut insts = insts;
    insts.push(Inst::End);
    Func { ty_idx, locals: vec![], insts }
}

/// Synthetic module: all sections present (type/function/table/memory/global/export/start/element/data/
/// datacount); function f1 contains data.drop (exercising the DataCount rule path).
fn rich_module() -> Module {
    let mut m = Module::default();
    m.types = vec![ty(&[], &[]), ty(&[ValType::I32], &[])];
    m.defined_func_ty_ids = vec![0, 0, 0];
    m.defined_funcs = vec![
        func(0, vec![Inst::Nop]),
        func(0, vec![Inst::DataDrop { data_index: 0 }, Inst::Drop]),
        func(0, vec![Inst::I32Const { value: 1 }]),
    ];
    m.defined_table_datas = vec![Table {
        ty: TableType { elem_ty: RefType::FUNCREF, limits: Limits { min: 4, max: None } },
        init_expr: None,
    }];
    m.defined_memory_datas =
        vec![Memory { ty: MemType { limits: Limits { min: 1, max: Some(4) } } }];
    m.defined_globals = vec![Global {
        ty: GlobalType { val_ty: ValType::I32, mutable: false },
        init_expr: vec![Inst::I32Const { value: 3 }, Inst::End],
    }];
    m.exports = vec![
        Export { name: "a".into(), desc: ExportDesc::Func(0) },
        Export { name: "m".into(), desc: ExportDesc::Memory(0) },
    ];
    m.start_sec_data = Some(2);
    m.elem_sec_datas = vec![ElemSeg {
        mode: ElemMode::Passive,
        payload: ElemPayload::FuncIdxs(vec![0, 1]),
    }];
    m.data_sec_datas = vec![
        DataSeg { mode: DataMode::Passive, data: vec![7; 24] },
        DataSeg {
            mode: DataMode::Active {
                mem_idx: 0,
                offset: vec![Inst::I32Const { value: 0 }, Inst::End],
            },
            data: vec![9; 8],
        },
    ];
    m.data_count_sec_data = Some(2);
    m
}

fn assert_parity(snap: &Snapshot, batch: &[Mutation], name: &str) {
    let a = apply_mutations(snap, batch)
        .unwrap_or_else(|e| panic!("{name}: direct path failed: {e:#}"));
    let b = apply_mutations_reference(snap, batch)
        .unwrap_or_else(|e| panic!("{name}: reference path failed: {e:#}"));
    let (ba, bb) = (a.encode_to_bytes().unwrap(), b.encode_to_bytes().unwrap());
    assert_eq!(ba, bb, "{name}: byte-passthrough vs re-encode differ (len {} vs {})", ba.len(), bb.len());
}

#[test]
fn parity_synthetic_matrix() {
    let snap = Snapshot::from_module(rich_module());
    let m = snap.module();

    // Identity (empty mutations).
    assert_parity(&snap, &[], "identity");

    // Deletions.
    assert_parity(&snap, &[Mutation::definitions(SK::Global, vec![DefEdit::delete(0)])], "del global");
    assert_parity(&snap, &[Mutation::definitions(SK::Export, vec![DefEdit::delete(0)])], "del export");
    assert_parity(&snap, &[Mutation::definitions(SK::Data, vec![DefEdit::delete(0)])], "del data0");
    // Delete all data segments → the DataCount section disappears (the P-16 mutation-channel rule).
    // Note: not compared against the reference here — the old path (clone-and-reencode) would run the
    // mutation-channel result through the parser2wasm rule again, where the "leftover data.drop instruction"
    // revives DataCount with value 0 (Python's mutation-channel output does not; the pass-through behavior is correct).
    {
        let batch = vec![Mutation::definitions(
            SK::Data,
            vec![DefEdit::delete(0), DefEdit::delete(1)],
        )];
        let a = apply_mutations(&snap, &batch).unwrap();
        let bytes = a.encode_to_bytes().unwrap();
        // Assert the output has no id=12 (DataCount) section: a quick section scan.
        let mut pos = 8usize;
        let mut has_dc = false;
        while pos < bytes.len() {
            let id = bytes[pos];
            pos += 1;
            let mut shift = 0u32;
            let mut len: u64 = 0;
            loop {
                let b = bytes[pos];
                pos += 1;
                len |= ((b & 0x7f) as u64) << shift;
                if b & 0x80 == 0 { break; }
                shift += 7;
            }
            if id == 12 { has_dc = true; }
            pos += len as usize;
        }
        assert!(!has_dc, "DataCount section must disappear when all data segs are removed (P-16 channel rule)");
    }
    assert_parity(&snap, &[Mutation::definitions(SK::Element, vec![DefEdit::delete(0)])], "del elem");
    assert_parity(&snap, &[Mutation::definitions(SK::Table, vec![DefEdit::delete(0)])], "del table");
    assert_parity(&snap, &[Mutation::definitions(SK::Memory, vec![DefEdit::delete(0)])], "del memory");
    // Function deletion = Function + Code deleted together.
    assert_parity(
        &snap,
        &[
            Mutation::definitions(SK::Function, vec![DefEdit::delete(0)]),
            Mutation::definitions(SK::Code, vec![DefEdit::delete(0)]),
        ],
        "del func0",
    );
    // start deletion.
    assert_parity(&snap, &[Mutation::definitions(SK::Start, vec![DefEdit::delete(0)])], "del start");
    // Type deletion (the function section's type indices are rewritten for the remaining types).
    assert_parity(
        &snap,
        &[
            Mutation::definitions(SK::Type, vec![DefEdit::delete(0)]),
            Mutation::definitions(SK::Function, vec![DefEdit::replace_one(0, u32_def(0))]),
            Mutation::definitions(SK::Function, vec![DefEdit::replace_one(1, u32_def(0))]),
            Mutation::definitions(SK::Function, vec![DefEdit::replace_one(2, u32_def(0))]),
        ],
        "del type0",
    );

    // Replacements.
    assert_parity(
        &snap,
        &[Mutation::definitions(SK::Export, vec![DefEdit::replace_one(0, export_def(&m.exports[0]).unwrap())])],
        "replace export0",
    );
    assert_parity(
        &snap,
        &[Mutation::definitions(SK::Data, vec![DefEdit::replace_one(0, data_def(&m.data_sec_datas[0]).unwrap())])],
        "replace data0",
    );
    let mut f = m.defined_funcs[0].clone();
    f.insts.insert(0, Inst::Nop);
    assert_parity(
        &snap,
        &[Mutation::definitions(SK::Code, vec![DefEdit::replace_one(0, func_def(&f).unwrap())])],
        "replace func0",
    );
    // start renumbering.
    assert_parity(
        &snap,
        &[Mutation::definitions(SK::Start, vec![DefEdit::replace_one(0, u32_def(1))])],
        "start -> 1",
    );

    // DataCount creation rule: replace in a function containing data.drop on a module without datacount.
    let mut m2 = rich_module();
    m2.data_count_sec_data = None;
    let snap2 = Snapshot::from_module(m2);
    assert_parity(
        &snap2,
        &[Mutation::definitions(SK::Code, vec![DefEdit::replace_one(0, func_def(&snap2.module().defined_funcs[1]).unwrap())])],
        "datacount introduced by ndc inst",
    );

    // Combinations: many sections mutated at once.
    assert_parity(
        &snap,
        &[
            Mutation::definitions(SK::Export, vec![DefEdit::delete(1)]),
            Mutation::definitions(SK::Data, vec![DefEdit::delete(1)]),
            Mutation::definitions(SK::Global, vec![DefEdit::delete(0)]),
            Mutation::definitions(SK::Start, vec![DefEdit::delete(0)]),
        ],
        "combined",
    );
}

#[test]
fn parity_corpus() {
    let tt_pat = format!("{}/CP9201/CP9201_MAIN/tt/*.wasm", std::env::var("HOME").unwrap_or_default());
    let mut files: Vec<PathBuf> = glob::glob(&tt_pat)
        .unwrap()
        .filter_map(Result::ok)
        .collect();
    files.sort();
    let files: Vec<PathBuf> = files.into_iter().step_by(29).take(10).collect();
    assert!(files.len() >= 5);
    for f in files {
        let snap = Snapshot::from_path(&f).unwrap();
        let m = snap.module();
        let mut batch: Vec<Mutation> = vec![];
        // Per section (if non-empty): delete the last entry + replace the first with the original content (equal-value replacement still goes through re-encoding).
        let del_last = |kind: SectionKind, n: usize, batch: &mut Vec<Mutation>| {
            if n > 1 {
                batch.push(Mutation::definitions(kind, vec![DefEdit::delete(n as u32 - 1)]));
            }
        };
        del_last(SK::Type, m.types.len(), &mut batch);
        del_last(SK::Export, m.exports.len(), &mut batch);
        del_last(SK::Global, m.defined_globals.len(), &mut batch);
        del_last(SK::Data, m.data_sec_datas.len(), &mut batch);
        del_last(SK::Element, m.elem_sec_datas.len(), &mut batch);
        if let Some(e) = m.exports.first() {
            batch.push(Mutation::definitions(
                SK::Export,
                vec![DefEdit::replace_one(0, export_def(e).unwrap())],
            ));
        }
        if let Some(d) = m.data_sec_datas.first() {
            batch.push(Mutation::definitions(SK::Data, vec![DefEdit::replace_one(0, data_def(d).unwrap())]));
        }
        if let Some(g) = m.defined_funcs.first() {
            batch.push(Mutation::definitions(SK::Code, vec![DefEdit::replace_one(0, func_def(g).unwrap())]));
        }
        if batch.is_empty() {
            continue;
        }
        assert_parity(&snap, &batch, &f.file_name().unwrap().to_string_lossy());
    }
}
