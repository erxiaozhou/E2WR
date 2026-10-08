//! M2 synthetic unit tests: small hand-built modules validating decode fields and encoding.

use e2wr_ir::decode::decode_bytes;
use e2wr_ir::encode::encode_module;
use e2wr_ir::inst::Inst;
use e2wr_ir::module::*;

fn build_tiny_module() -> wasm_encoder::Module {
        use wasm_encoder::*;

    let mut m = Module::new();
    let mut types = TypeSection::new();
    types.ty().function(vec![], vec![ValType::I32]);
    m.section(&types);

    let mut imports = ImportSection::new();
    imports.import("env", "log", EntityType::Function(0));
    imports.import(
        "env",
        "mem",
        EntityType::Memory(MemoryType {
            minimum: 1,
            maximum: None,
            shared: false,
            memory64: false,
            page_size_log2: None,
        }),
    );
    m.section(&imports);

    let mut funcs = FunctionSection::new();
    funcs.function(0);
    m.section(&funcs);

    let mut tables = TableSection::new();
    tables.table(TableType {
        element_type: RefType::FUNCREF,
        minimum: 1,
        maximum: Some(2),
        shared: false,
        table64: false,
    });
    m.section(&tables);

    let mut globals = GlobalSection::new();
    globals.global(
        GlobalType {
            val_type: ValType::I32,
            mutable: true,
            shared: false,
        },
        &ConstExpr::i32_const(7),
    );
    m.section(&globals);

    let mut exports = ExportSection::new();
    exports.export("main", ExportKind::Func, 1);
    m.section(&exports);

    let mut elems = ElementSection::new();
    elems.segment(ElementSegment {
        mode: ElementMode::Active {
            table: None,
            offset: &ConstExpr::i32_const(0),
        },
        elements: Elements::Functions(std::borrow::Cow::Borrowed(&[1])),
    });
    m.section(&elems);

    let mut code = CodeSection::new();
    let mut f = Function::new(vec![(1, ValType::I64)]);
    f.instruction(&Instruction::I32Const(42))
        .instruction(&Instruction::Call(0))
        .instruction(&Instruction::I64Const(-1))
        .instruction(&Instruction::Drop)
        .instruction(&Instruction::End);
    code.function(&f);
    m.section(&code);

    let mut data = DataSection::new();
    data.active(0, &ConstExpr::i32_const(0), b"hi\0".iter().copied());
    m.section(&data);
    m
}

#[test]
fn decode_small_module_fields() {
    let bytes = build_tiny_module().finish();
    let m = decode_bytes(&bytes).unwrap();
    assert_eq!(m.types.len(), 1);
    assert!(m.types[0].params.is_empty());
    assert_eq!(m.imports.len(), 2);
    assert!(matches!(&m.imports[0].desc, ImportDesc::Func(0)));
    assert_eq!(m.defined_func_ty_ids, vec![0]);
    assert_eq!(m.exports.len(), 1);
    assert_eq!(m.exports[0].name, "main");
    assert!(matches!(m.exports[0].desc, ExportDesc::Func(1)));
    assert_eq!(m.defined_table_datas.len(), 1);
    assert_eq!(m.defined_table_datas[0].ty.limits.min, 1);
    assert_eq!(m.defined_table_datas[0].ty.limits.max, Some(2));
    assert_eq!(m.defined_globals.len(), 1);
    assert!(m.defined_globals[0].ty.mutable);
    assert_eq!(m.elem_sec_datas.len(), 1);
    match &m.elem_sec_datas[0].payload {
        ElemPayload::FuncIdxs(v) => assert_eq!(v, &vec![1]),
        other => panic!("expected funcidx elem, got {other:?}"),
    }
    assert_eq!(m.defined_funcs.len(), 1);
    assert_eq!(m.defined_funcs[0].locals.len(), 1);
    assert_eq!(m.defined_funcs[0].insts.len(), 5);
    assert!(matches!(m.defined_funcs[0].insts[0], Inst::I32Const { value: 42 }));
    assert!(matches!(m.defined_funcs[0].insts[1], Inst::Call { function_index: 0 }));
    assert_eq!(m.data_sec_datas.len(), 1);
    assert_eq!(m.data_sec_datas[0].data, b"hi\0");
    // import memory: memory section empty, one memory among the imports
    assert_eq!(m.import_memory_num(), 1);
    assert_eq!(m.import_func_num(), 1);
    assert_eq!(m.func_type_idxs(), vec![0, 0]);
}

#[test]
fn roundtrip_small_module_validate() {
    let bytes = build_tiny_module().finish();
    let m = decode_bytes(&bytes).unwrap();
    let out = encode_module(&m).unwrap();
    let m2 = decode_bytes(&out).unwrap();
    assert_eq!(m, m2, "module IR must survive a roundtrip");
    let out2 = encode_module(&m2).unwrap();
    assert_eq!(out, out2);
}

#[test]
fn custom_sections_keep_only_last() {
    // Actual Python behavior (P-15): only the last of multiple custom sections survives.
    use wasm_encoder::*;
    let mut m = Module::new();
    m.section(&CustomSection {
        name: std::borrow::Cow::Borrowed("first"),
        data: std::borrow::Cow::Borrowed(b"aaaa"),
    });
    let mut types = TypeSection::new();
    types.ty().function(vec![], vec![]);
    m.section(&types);
    let mut funcs = FunctionSection::new();
    funcs.function(0);
    m.section(&funcs);
    let mut code = CodeSection::new();
    code.function(Function::new(vec![]).instruction(&Instruction::End));
    m.section(&code);
    m.section(&CustomSection {
        name: std::borrow::Cow::Borrowed("name"),
        data: std::borrow::Cow::Borrowed(b"bbbb"),
    });
    let bytes = m.finish();
    let decoded = decode_bytes(&bytes).unwrap();
    assert_eq!(decoded.customs.len(), 1);
    assert_eq!(decoded.customs[0].name, "name");
    // custom sections encode first (seq_encode_seq order)
    let re = encode_module(&decoded).unwrap();
    assert_eq!(re[8], 0, "custom section encoded first after header");
    let name_len = re[10] as usize;
    assert_eq!(&re[11..11 + name_len], b"name");
}

#[test]
fn data_count_rule_follows_parser2wasm() {
    // No data.drop/memory.init and no original data_count section: stays absent.
    use wasm_encoder::*;
    let mut m = Module::new();
    let mut types = TypeSection::new();
    types.ty().function(vec![], vec![]);
    m.section(&types);
    let mut funcs = FunctionSection::new();
    funcs.function(0);
    m.section(&funcs);
    let mut code = CodeSection::new();
    code.function(Function::new(vec![]).instruction(&Instruction::End));
    m.section(&code);
    let decoded = decode_bytes(&m.finish()).unwrap();
    assert!(decoded.data_count_sec_data.is_none());
    let out = encode_module(&decoded).unwrap();
    let decoded2 = decode_bytes(&out).unwrap();
    assert!(decoded2.data_count_sec_data.is_none());
}

#[test]
fn br_table_roundtrip() {
    use wasm_encoder::*;
    let mut m = Module::new();
    let mut types = TypeSection::new();
    types.ty().function(vec![], vec![]);
    m.section(&types);
    let mut funcs = FunctionSection::new();
    funcs.function(0);
    m.section(&funcs);
    let mut code = CodeSection::new();
    code.function(
        Function::new(vec![])
            .instruction(&Instruction::I32Const(0))
            .instruction(&Instruction::BrTable(
                std::borrow::Cow::Owned(vec![0u32, 1, 2]),
                3,
            ))
            .instruction(&Instruction::Block(wasm_encoder::BlockType::Empty))
            .instruction(&Instruction::Block(wasm_encoder::BlockType::Empty))
            .instruction(&Instruction::Block(wasm_encoder::BlockType::Empty))
            .instruction(&Instruction::End)
            .instruction(&Instruction::End)
            .instruction(&Instruction::End)
            .instruction(&Instruction::End),
    );
    m.section(&code);
    let decoded = decode_bytes(&m.finish()).unwrap();
    match &decoded.defined_funcs[0].insts[1] {
        Inst::BrTable(d) => {
            assert_eq!(d.targets, vec![0, 1, 2]);
            assert_eq!(d.default, 3);
        }
        other => panic!("expected br_table, got {other:?}"),
    }
    let out = encode_module(&decoded).unwrap();
    let decoded2 = decode_bytes(&out).unwrap();
    assert_eq!(decoded, decoded2);
}
