//! Encoding: Module → wasm bytes (M2.3).
//!
//! Mirrors `parser2wasm`/`parser2wasm_core` from Python `extract_block_mutator/parser_to_file_util.py`
//! plus `seq_encode_seq` from `util/prepare_template.py`.
//!
//! Behavior notes:
//! - Section order follows `seq_encode_seq`: custom first, the rest in standard order (data_count before code);
//! - empty vector sections are skipped; missing start/data_count are skipped, and their contents carry no vector count prefix;
//! - DataCount consistency rule (parser2wasm): when a function body contains data.drop/memory.init,
//!   data_count = the number of data segments; otherwise, if data_count exists and the data section is non-empty, refresh it to the count;
//!   otherwise keep it as is (including a stale data_count left after the data section was emptied — actual Python behavior).
//!
//! Structural difference (D-6): Python recomputes function type indices from `wasmFunc.func_ty` objects while encoding
//! and may append new types; Rust's `Func` stores the type index, and encoding uses
//! `defined_func_ty_ids` directly — index consistency is maintained by the reducer.

use anyhow::Result;
use wasmparser::ValType;

use crate::inst::Inst;
use crate::module::*;

fn rt_val_type(t: ValType) -> wasm_encoder::ValType {
    let mut rt = wasm_encoder::reencode::RoundtripReencoder;
    use wasm_encoder::reencode::Reencode;
    rt.val_type(t).unwrap()
}

fn rt_ref_type(t: wasmparser::RefType) -> wasm_encoder::RefType {
    let mut rt = wasm_encoder::reencode::RoundtripReencoder;
    use wasm_encoder::reencode::Reencode;
    rt.ref_type(t).unwrap()
}

fn enc_table_type(t: &TableType) -> wasm_encoder::TableType {
    wasm_encoder::TableType {
        element_type: rt_ref_type(t.elem_ty),
        minimum: t.limits.min,
        maximum: t.limits.max,
        shared: false,
        table64: false,
    }
}

fn enc_mem_type(t: &MemType) -> wasm_encoder::MemoryType {
    wasm_encoder::MemoryType {
        minimum: t.limits.min,
        maximum: t.limits.max,
        shared: false,
        memory64: false,
        page_size_log2: None,
    }
}

fn enc_global_type(t: &GlobalType) -> wasm_encoder::GlobalType {
    wasm_encoder::GlobalType {
        val_type: rt_val_type(t.val_ty),
        mutable: t.mutable,
        shared: false,
    }
}

fn enc_expr(insts: &[Inst]) -> wasm_encoder::ConstExpr {
    // Constant expressions on the decode side carry a trailing `end`, while wasm-encoder's ConstExpr
    // appends `end` automatically (impl Encode for ConstExpr) — drop the decoded trailing
    // `end` here to avoid duplication.
    let insts = match insts.last() {
        Some(Inst::End) => &insts[..insts.len() - 1],
        _ => insts,
    };
    wasm_encoder::ConstExpr::extended(insts.iter().map(|i| i.to_instruction()))
}

fn func_body_uses_data_count(insts: &[Inst]) -> bool {
    insts.iter().any(|i| matches!(i, Inst::DataDrop { .. } | Inst::MemoryInit { .. }))
}

/// Encodes a whole module (parser2wasm semantics).
pub fn encode_module(module: &Module) -> Result<Vec<u8>> {
    let mut m = wasm_encoder::Module::new();

    // DataCount consistency (see the module comment). The two branches act identically but their condition semantics differ; kept explicit
    // to align with the structure of the Python source.
    let mut data_count = module.data_count_sec_data;
    let need_insert_data_count = module
        .defined_funcs
        .iter()
        .any(|f| func_body_uses_data_count(&f.insts));
    let data_seg_num = module.data_sec_datas.len() as u32;
    #[allow(clippy::if_same_then_else)]
    if need_insert_data_count {
        data_count = Some(data_seg_num);
    } else if data_seg_num > 0 && data_count.is_some() {
        data_count = Some(data_seg_num);
    }

    // custom sections (Python actually keeps only the last one, see P-15 on the decode side; encoded as a list here).
    for c in &module.customs {
        m.section(&wasm_encoder::CustomSection {
            name: std::borrow::Cow::Borrowed(&c.name),
            data: std::borrow::Cow::Borrowed(&c.data),
        });
    }

    if !module.types.is_empty() {
        let mut s = wasm_encoder::TypeSection::new();
        for ty in &module.types {
            s.ty().function(
                ty.params.iter().map(|t| rt_val_type(*t)).collect::<Vec<_>>(),
                ty.results.iter().map(|t| rt_val_type(*t)).collect::<Vec<_>>(),
            );
        }
        m.section(&s);
    }

    if !module.imports.is_empty() {
        let mut s = wasm_encoder::ImportSection::new();
        for imp in &module.imports {
            let ty = match &imp.desc {
                ImportDesc::Func(ty_idx) => wasm_encoder::EntityType::Function(*ty_idx),
                ImportDesc::Table(t) => {
                    wasm_encoder::EntityType::Table(enc_table_type(t))
                }
                ImportDesc::Memory(t) => {
                    wasm_encoder::EntityType::Memory(enc_mem_type(t))
                }
                ImportDesc::Global(t) => {
                    wasm_encoder::EntityType::Global(enc_global_type(t))
                }
            };
            s.import(&imp.module, &imp.name, ty);
        }
        m.section(&s);
    }

    if !module.defined_func_ty_ids.is_empty() {
        let mut s = wasm_encoder::FunctionSection::new();
        for ty_idx in &module.defined_func_ty_ids {
            s.function(*ty_idx);
        }
        m.section(&s);
    }

    if !module.defined_table_datas.is_empty() {
        let mut s = wasm_encoder::TableSection::new();
        for t in &module.defined_table_datas {
            match &t.init_expr {
                None => s.table(enc_table_type(&t.ty)),
                Some(expr) => s.table_with_init(enc_table_type(&t.ty), &enc_expr(expr)),
            };
        }
        m.section(&s);
    }

    if !module.defined_memory_datas.is_empty() {
        let mut s = wasm_encoder::MemorySection::new();
        for mem in &module.defined_memory_datas {
            s.memory(enc_mem_type(&mem.ty));
        }
        m.section(&s);
    }

    if !module.defined_globals.is_empty() {
        let mut s = wasm_encoder::GlobalSection::new();
        for g in &module.defined_globals {
            s.global(enc_global_type(&g.ty), &enc_expr(&g.init_expr));
        }
        m.section(&s);
    }

    if !module.exports.is_empty() {
        let mut s = wasm_encoder::ExportSection::new();
        for e in &module.exports {
            let (kind, idx) = match e.desc {
                ExportDesc::Func(i) => (wasm_encoder::ExportKind::Func, i),
                ExportDesc::Table(i) => (wasm_encoder::ExportKind::Table, i),
                ExportDesc::Memory(i) => (wasm_encoder::ExportKind::Memory, i),
                ExportDesc::Global(i) => (wasm_encoder::ExportKind::Global, i),
            };
            s.export(&e.name, kind, idx);
        }
        m.section(&s);
    }

    if let Some(start) = module.start_sec_data {
        m.section(&wasm_encoder::StartSection { function_index: start });
    }

    if !module.elem_sec_datas.is_empty() {
        let mut s = wasm_encoder::ElementSection::new();
        for e in &module.elem_sec_datas {
            let elements = match &e.payload {
                ElemPayload::FuncIdxs(idxs) => wasm_encoder::Elements::Functions(
                    std::borrow::Cow::Borrowed(idxs),
                ),
                ElemPayload::Exprs { elem_ty, exprs } => wasm_encoder::Elements::Expressions(
                    rt_ref_type(*elem_ty),
                    exprs.iter().map(|x| enc_expr(x)).collect(),
                ),
            };
            let mode = match &e.mode {
                ElemMode::Passive => wasm_encoder::ElementMode::Passive,
                ElemMode::Declarative => wasm_encoder::ElementMode::Declared,
                ElemMode::Active { table_idx, offset } => {
                    wasm_encoder::ElementMode::Active {
                        table: *table_idx,
                        offset: &enc_expr(offset),
                    }
                }
            };
            s.segment(wasm_encoder::ElementSegment { mode, elements });
        }
        m.section(&s);
    }

    if let Some(count) = data_count {
        m.section(&wasm_encoder::DataCountSection { count });
    }

    if !module.defined_funcs.is_empty() {
        let mut s = wasm_encoder::CodeSection::new();
        for func in &module.defined_funcs {
            let locals = compress_locals(&func.locals);
            let mut f = wasm_encoder::Function::new(
                locals.into_iter().map(|(c, t)| (c, rt_val_type(t))).collect::<Vec<_>>(),
            );
            for inst in &func.insts {
                inst.write(&mut f);
            }
            s.function(&f);
        }
        m.section(&s);
    }

    if !module.data_sec_datas.is_empty() {
        let mut s = wasm_encoder::DataSection::new();
        for d in &module.data_sec_datas {
            match &d.mode {
                DataMode::Passive => {
                    s.passive(d.data.iter().copied());
                }
                DataMode::Active { mem_idx, offset } => {
                    s.active(*mem_idx, &enc_expr(offset), d.data.iter().copied());
                }
            }
        }
        m.section(&s);
    }

    Ok(m.finish())
}

/// Expanded local variable sequence → (type, count) declaration form (the inverse of Python `locals_with_def_repr`).
fn compress_locals(locals: &[ValType]) -> Vec<(u32, ValType)> {
    let mut out: Vec<(u32, ValType)> = Vec::new();
    for t in locals {
        match out.last_mut() {
            Some((cnt, last)) if *last == *t => *cnt += 1,
            _ => out.push((1, *t)),
        }
    }
    out
}
