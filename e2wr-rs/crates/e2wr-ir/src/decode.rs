//! Decoding: wasm bytes → Module (M2.2).
//!
//! Mirrors `WasmParser.get_parser_from_wasm_path` + `prepare_sec_name2all_ba`.
//! Behavioral quirk replicated (P-15): Python collects sections in a "section name → section bytes" dict,
//! where a later same-name section (including multiple custom sections) overwrites the earlier one, so **only the last custom section survives**;
//! Rust does the same. Duplicate non-custom sections are invalid input, also handled by overwriting.

use anyhow::{anyhow, bail, Result};

use crate::inst::Inst;
use crate::module::*;

// R-26: a convenience decode_path(path) bypassing the snapshot chain had zero callers repo-wide
// (Snapshot::from_path uses the private decode_path_read); removed.
//

pub(crate) fn conv_mem_type(t: wasmparser::MemoryType) -> MemType {
    MemType {
        limits: Limits {
            min: t.initial,
            max: t.maximum,
        },
    }
}

pub(crate) fn conv_table_type(t: wasmparser::TableType) -> TableType {
    TableType {
        elem_ty: t.element_type,
        limits: Limits {
            min: t.initial,
            max: t.maximum,
        },
    }
}

pub(crate) fn conv_global_type(t: wasmparser::GlobalType) -> GlobalType {
    GlobalType {
        val_ty: t.content_type,
        mutable: t.mutable,
    }
}

pub(crate) fn conv_expr(expr: wasmparser::ConstExpr<'_>) -> Result<Vec<Inst>> {
    let mut ops = expr.get_operators_reader();
    let mut insts = Vec::new();
    while !ops.eof() {
        let op = ops.read()?;
        insts.push(Inst::from_operator(op));
    }
    Ok(insts)
}

pub(crate) fn conv_table_init(init: wasmparser::TableInit<'_>) -> Result<Option<Vec<Inst>>> {
    match init {
        wasmparser::TableInit::RefNull => Ok(None),
        wasmparser::TableInit::Expr(expr) => Ok(Some(conv_expr(expr)?)),
    }
}

/// Decodes a byte array.
pub fn decode_bytes(bytes: &[u8]) -> Result<Module> {
    let mut module = Module::default();
    for payload in wasmparser::Parser::new(0).parse_all(bytes) {
        let payload = payload?;
        match payload {
            wasmparser::Payload::Version { encoding, .. } => {
                if encoding != wasmparser::Encoding::Module {
                    bail!("expected a module, not a component");
                }
            }
            wasmparser::Payload::TypeSection(r) => {
                module.types = Vec::with_capacity(r.count() as usize);
                for rec_group in r {
                    let rec_group = rec_group?;
                    for sub_ty in rec_group.types() {
                        // Accept plain final function types only (corpus range); GC subtypes error out.
                        if !sub_ty.is_final || sub_ty.supertype_idx.is_some() {
                            bail!("unsupported non-final/subtype entry in type section");
                        }
                        match &sub_ty.composite_type.inner {
                            wasmparser::CompositeInnerType::Func(ft) => {
                                module.types.push(FuncType {
                                    params: ft.params().to_vec(),
                                    results: ft.results().to_vec(),
                                });
                            }
                            other => bail!(
                                "unsupported type section entry {:?} (GC type; not expected in the corpus)",
                                std::mem::discriminant(other)
                            ),
                        }
                    }
                }
            }
            wasmparser::Payload::ImportSection(r) => {
                module.imports = Vec::with_capacity(r.count() as usize);
                for imp in r {
                    module.imports.push(conv_import(imp?)?);
                }
            }
            wasmparser::Payload::FunctionSection(r) => {
                module.defined_func_ty_ids = Vec::with_capacity(r.count() as usize);
                for ty_idx in r {
                    module.defined_func_ty_ids.push(ty_idx?);
                }
            }
            wasmparser::Payload::TableSection(r) => {
                module.defined_table_datas = Vec::with_capacity(r.count() as usize);
                for t in r {
                    module.defined_table_datas.push(conv_table(t?)?);
                }
            }
            wasmparser::Payload::MemorySection(r) => {
                module.defined_memory_datas = Vec::with_capacity(r.count() as usize);
                for m in r {
                    module.defined_memory_datas.push(Memory { ty: conv_mem_type(m?) });
                }
            }
            wasmparser::Payload::GlobalSection(r) => {
                module.defined_globals = Vec::with_capacity(r.count() as usize);
                for g in r {
                    module.defined_globals.push(conv_global(g?)?);
                }
            }
            wasmparser::Payload::ExportSection(r) => {
                module.exports = Vec::with_capacity(r.count() as usize);
                for e in r {
                    module.exports.push(conv_export(e?)?);
                }
            }
            wasmparser::Payload::StartSection { func, .. } => {
                module.start_sec_data = Some(func);
            }
            wasmparser::Payload::ElementSection(r) => {
                module.elem_sec_datas = Vec::with_capacity(r.count() as usize);
                for e in r {
                    let e = e?;
                    module.elem_sec_datas.push(conv_element(e)?);
                }
            }
            wasmparser::Payload::CodeSectionStart { .. } => {}
            wasmparser::Payload::CodeSectionEntry(body) => {
                // ty_idx is first filled with the u32::MAX placeholder and patched at the end of the section (Python assembly-order constraint).
                module.defined_funcs.push(conv_func_body(body)?);
            }
            wasmparser::Payload::DataSection(r) => {
                module.data_sec_datas = Vec::with_capacity(r.count() as usize);
                for d in r {
                    module.data_sec_datas.push(conv_data(d?)?);
                }
            }
            wasmparser::Payload::DataCountSection { count, .. } => {
                module.data_count_sec_data = Some(count);
            }
            wasmparser::Payload::CustomSection(c) => {
                // Actual Python behavior: same-name sections overwrite; only the last custom section survives (P-15).
                module.customs = vec![Custom {
                    name: c.name().to_string(),
                    data: c.data().to_vec(),
                }];
            }
            wasmparser::Payload::End(_) => {}
            _ => {
                bail!("unsupported wasm section payload: {:?}", payload);
            }
        }
    }
    // Function bodies arrive after the function section: patch the type indices back in (Python assembly-order constraint).
    if module.defined_funcs.len() != module.defined_func_ty_ids.len() {
        return Err(anyhow!(
            "function section ({}) and code section ({}) size mismatch",
            module.defined_func_ty_ids.len(),
            module.defined_funcs.len()
        ));
    }
    for (f, ty_idx) in module.defined_funcs.iter_mut().zip(&module.defined_func_ty_ids) {
        f.ty_idx = *ty_idx;
    }
    Ok(module)
}

pub(crate) fn conv_import(imp: wasmparser::Import<'_>) -> Result<Import> {
    let desc = match imp.ty {
        wasmparser::TypeRef::Func(ty_idx) => ImportDesc::Func(ty_idx),
        wasmparser::TypeRef::Table(t) => ImportDesc::Table(conv_table_type(t)),
        wasmparser::TypeRef::Memory(t) => ImportDesc::Memory(conv_mem_type(t)),
        wasmparser::TypeRef::Global(t) => ImportDesc::Global(conv_global_type(t)),
        wasmparser::TypeRef::Tag(_) => bail!("unsupported import kind Tag"),
    };
    Ok(Import {
        module: imp.module.to_string(),
        name: imp.name.to_string(),
        desc,
    })
}

pub(crate) fn conv_table(t: wasmparser::Table<'_>) -> Result<Table> {
    Ok(Table {
        ty: conv_table_type(t.ty),
        init_expr: conv_table_init(t.init)?,
    })
}

pub(crate) fn conv_global(g: wasmparser::Global<'_>) -> Result<Global> {
    Ok(Global {
        ty: conv_global_type(g.ty),
        init_expr: conv_expr(g.init_expr)?,
    })
}

pub(crate) fn conv_export(e: wasmparser::Export<'_>) -> Result<Export> {
    let desc = match e.kind {
        wasmparser::ExternalKind::Func => ExportDesc::Func(e.index),
        wasmparser::ExternalKind::Table => ExportDesc::Table(e.index),
        wasmparser::ExternalKind::Memory => ExportDesc::Memory(e.index),
        wasmparser::ExternalKind::Global => ExportDesc::Global(e.index),
        other => bail!("unsupported export kind {other:?}"),
    };
    Ok(Export { name: e.name.to_string(), desc })
}

pub(crate) fn conv_element(e: wasmparser::Element<'_>) -> Result<ElemSeg> {
    let mode = match e.kind {
        wasmparser::ElementKind::Passive => ElemMode::Passive,
        wasmparser::ElementKind::Declared => ElemMode::Declarative,
        wasmparser::ElementKind::Active { table_index, offset_expr } => ElemMode::Active {
            table_idx: table_index,
            offset: conv_expr(offset_expr)?,
        },
    };
    let payload = match e.items {
        wasmparser::ElementItems::Functions(r) => {
            let mut idxs = Vec::with_capacity(r.count() as usize);
            for idx in r {
                idxs.push(idx?);
            }
            ElemPayload::FuncIdxs(idxs)
        }
        wasmparser::ElementItems::Expressions(ty, r) => {
            let mut exprs = Vec::with_capacity(r.count() as usize);
            for ex in r {
                exprs.push(conv_expr(ex?)?);
            }
            ElemPayload::Exprs { elem_ty: ty, exprs }
        }
    };
    Ok(ElemSeg { mode, payload })
}

pub(crate) fn conv_data(d: wasmparser::Data<'_>) -> Result<DataSeg> {
    let mode = match d.kind {
        wasmparser::DataKind::Active { memory_index, offset_expr } => DataMode::Active {
            mem_idx: memory_index,
            offset: conv_expr(offset_expr)?,
        },
        wasmparser::DataKind::Passive => DataMode::Passive,
    };
    Ok(DataSeg { mode, data: d.data.to_vec() })
}

/// R-27: the ty_idx parameter was removed — the only 2 call sites (in-section decoding in decode_bytes,
/// single-definition decoding in mutation.rs) both passed the u32::MAX placeholder; the real type index is
/// patched back by the caller (Python assembly-order constraint), so a placeholder is always written here.
pub(crate) fn conv_func_body(body: wasmparser::FunctionBody<'_>) -> Result<Func> {
    let mut locals = Vec::new();
    let mut lr = body.get_locals_reader()?;
    for _ in 0..lr.get_count() {
        let (cnt, ty) = lr.read()?;
        for _ in 0..cnt {
            locals.push(ty);
        }
    }
    let mut ops = body.get_operators_reader()?;
    let mut insts = Vec::new();
    while !ops.eof() {
        insts.push(Inst::from_operator(ops.read()?));
    }
    Ok(Func { ty_idx: u32::MAX, locals, insts })
}
