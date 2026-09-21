from random import randint
import time
from .util import get_insts_num
from file_util import check_dir, copy_file
from .ReducerPassUtil.ReducePass import ReduceAndCheckPass, ReducePass, reduce_checker
from timeout_process import run_with_timeout
from typing import Callable, Union
from pathlib import Path
from .ReducerPassUtil.ReduceResult import ExecResult, ExecStatus, ReduceResult, determine_exec_status_from_run_cmd_output
from enum import Enum
from reduction_analysis.ReducerCommonConfig import PASS_TIMEOUT, WASM_OPT_PATH, WASM_TOOLS_PATH, PASS_TIMEOUT
from reduction_analysis.ReductionDescUtil.Oracle import CheckType, call_oracle
from .ReducerPassUtil.get_reduce_result_util import get_reduce_result



class CommandReducePass(ReducePass):
    def __init__(
        self, 
        name:str,
        cmd_fmt:str,
        tmp_dir:Union[str, Path],
        timeout:int=PASS_TIMEOUT,
    ):
        super().__init__(name, tmp_dir)
        self.cmd_fmt = cmd_fmt
        self.timeout = timeout

    @reduce_checker
    def reduce(self, 
                    input_path:Union[str, Path], 
                    output_path:Union[str, Path], 
                    cur_timeout:int=PASS_TIMEOUT,
                    )->ExecResult:
        assert Path(output_path).parent.exists(), f"output_path {output_path} does not exist"
        atcual_cmd = self._get_actual_command(input_path, output_path)
        
        actual_timeout = max(self.timeout, cur_timeout)
        # print('Actual command is ', atcual_cmd)
        return self._exec_core(atcual_cmd, actual_timeout, input_path, output_path)

    def _get_actual_command(self, input_path, output_path):
        return self.cmd_fmt.format(input_path, output_path)

    def _exec_core(self, atcual_cmd, actual_timeout, input_path, output_path):
        start_time = time.time()
        input_size = Path(input_path).stat().st_size
        ori_inst_num = get_insts_num(input_path)
        exec_output = run_with_timeout(atcual_cmd, actual_timeout)
        
        end_time = time.time()
        exec_status = determine_exec_status_from_run_cmd_output(exec_output)
        if exec_status == ExecStatus.TIMEOUT or exec_status == ExecStatus.EXEC_FAILED:
            print('Failed command is ', atcual_cmd)
            print('Execution failed stderr: ', exec_output['stderr'])
            reduced_size = None
            reduced_inst_num = None
        else:
            reduced_case_size = Path(output_path).stat().st_size
            reduced_size = input_size - reduced_case_size
            reduced_inst_num = ori_inst_num - get_insts_num(output_path)
        
        return ExecResult(
            exec_status=exec_status,
            exec_taken_time=end_time - start_time,
            reduced_size_num=reduced_size,
            reduced_inst_num=reduced_inst_num
        )

class NeedSeedCommandReducePass(CommandReducePass):
    def __init__(self, name: str, cmd_fmt: str, tmp_dir: str | Path, timeout: int = PASS_TIMEOUT):
        super().__init__(name, cmd_fmt, tmp_dir, timeout)

    
    def _get_actual_command(self, input_path, output_path):
        seed = randint(0, 1000000)
        return self.cmd_fmt.format(input_path, output_path, seed)

class CommandReducePassAndCheck(ReduceAndCheckPass):
    def __init__(
        self, 
        cmd_pass:CommandReducePass,
        pass_oracle_func:Callable,
        require_smaller_size:bool=True,
    ):
        super().__init__(
            name=cmd_pass.name,
            reduce_pass=cmd_pass,
            pass_oracle_func=pass_oracle_func,
        )
        self.require_smaller_size = require_smaller_size
    
    def reduce_and_check(self, 
                         input_path:str, 
                         output_path:str, 
                         timeout:int=PASS_TIMEOUT
                         )->ReduceResult:
        start_time = time.time()
        exec_result: ExecResult = self.reduce_pass.reduce(input_path, output_path, timeout)
        end_time: float = time.time()
        time_cost = end_time - start_time
        result = get_reduce_result(
            output_path, 
            exec_result,
            self.pass_oracle_func,
            time_cost, 
            self.require_smaller_size
            )
        # print('exec output path is ', output_path)
        print(f'{self.name} Input / Reduced : {Path(input_path).stat().st_size} / {exec_result.reduced_size_num} /{result.reduce_process_status.name}      ')
        return result


class WASM_OPT_CMDName(Enum):
    OZ = "Oz"  
    OS = "Os"
    O1 = "O1"
    O2 = "O2"
    O3 = "O3"
    O4 = "O4"
    
    FLATTEN_OS = "flatten_os"
    FLATTEN_O3 = "flatten_o3"
    FLATTEN_SIMPLIFY_LOCAL_CSE_OS = "flatten_simplify_local_cse_os"
    TYPE_SSA_OS_TYPE_MERGING = "type_ssa_os_type_merging"
    GUFA_O1 = "gufa_o1"
    
    COALESCE_LOCALS_VACUUM = "coalesce_locals_vacuum"
    DAE = "dae"
    DAE_OPTIMIZING = "dae_optimizing"
    DCE = "dce"
    DUPLICATE_FUNCTION_ELIMINATION = "duplicate_fun ction_elimination"
    ENCLOSE_WORLD = "enclose_world"
    GTO = "gto"
    INLINING = "inlining"
    INLINING_OPTIMIZING = "inlining_optimizing"
    OPTIMIZE_LEVEL3_INLINING_OPTIMIZING = "optimize_level3_inlining_optimizing"
    LOCAL_CSE = "local_cse"
    MEMORY_PACKING = "memory_packing"
    REMOVE_UNUSED_NAMES_MERGE_BLOCKS_VACUUM = "remove_unused_names_merge_blocks_vacuum"
    OPTIMIZE_INSTRUCTIONS = "optimize_instructions"
    PRECOMPUTE = "precompute"
    REMOVE_IMPORTS = "remove_imports"
    REMOVE_MEMORY_INIT = "remove_memory_init"
    REMOVE_UNUSED_NAMES_UNUSED_BRS = "remove_unused_names_unused_brs"
    REMOVE_UNUSED_MODULE_ELEMENTS = "remove_unused_module_elements"
    REMOVE_UNUSED_NONFUNCTION_MODULE_ELEMENTS = "remove_unused_nonfunction_module_elements"
    REORDER_FUNCTIONS = "reorder_functions"
    REORDER_LOCALS = "reorder_locals"
    SIMPLIFY_GLOBALS = "simplify_globals"
    SIMPLIFY_LOCALS_VACUUM = "simplify_locals_vacuum"
    STRIP = "strip"
    REMOVE_UNUSED_TYPES_CLOSED_WORLD = "remove_unused_types_closed_world"
    VACUUM = "vacuum"


class WASM_MUTATE_CMDName(Enum):
    WASM_TOOLS_PEEPHOLE = "wasm_tools_peephole"
    WASM_TOOLS_REMOVE_EXPORT = "wasm_tools_remove_export"
    WASM_TOOLS_RENAME_EXPORT = "wasm_tools_rename_export"
    WASM_TOOLS_SNIP = "wasm_tools_snip"
    WASM_TOOLS_CODEMOTION = "wasm_tools_codemotion"
    WASM_TOOLS_FUNCTION_BODY_UNREACHABLE = "wasm_tools_function_body_unreachable"
    WASM_TOOLS_REMOVE_SECTION_CUSTOM = "wasm_tools_remove_section_custom"
    WASM_TOOLS_REMOVE_SECTION_EMPTY = "wasm_tools_remove_section_empty"
    WASM_TOOLS_REMOVE_ITEM_FUNCTION = "wasm_tools_remove_item_function"
    WASM_TOOLS_REMOVE_ITEM_GLOBAL = "wasm_tools_remove_item_global"
    WASM_TOOLS_REMOVE_ITEM_MEMORY = "wasm_tools_remove_item_memory"
    WASM_TOOLS_REMOVE_ITEM_TABLE = "wasm_tools_remove_item_table"
    WASM_TOOLS_REMOVE_ITEM_TYPE = "wasm_tools_remove_item_type"
    WASM_TOOLS_REMOVE_ITEM_DATA = "wasm_tools_remove_item_data"
    WASM_TOOLS_REMOVE_ITEM_ELEMENT = "wasm_tools_remove_item_element"
    WASM_TOOLS_REMOVE_ITEM_TAG = "wasm_tools_remove_item_tag"


wasm_opt_extra_flags = '--enable-mutable-globals --enable-simd --enable-nontrapping-float-to-int --enable-bulk-memory --enable-multivalue --enable-reference-types '


class MultiRunCmdPass(NeedSeedCommandReducePass):
    def __init__(
        self, 
        name:str,
        cmd_fmt:str,
        oracle_func:Callable,
        tmp_dir:Union[str, Path],
        timeout:int=PASS_TIMEOUT,
        max_try_times:int=5
    ):
        # super().__init__(name, tmp_dir)
        super().__init__(name, cmd_fmt, tmp_dir, timeout)
        self.max_try_times = max_try_times
        self.tmp_dir = check_dir(tmp_dir)
        self.tmp_output_path = self.tmp_dir / 'tmp_phase_output.wasm'
        self.oracle_func = oracle_func
    @reduce_checker
    def reduce(self, input_path, output_path, timeout=PASS_TIMEOUT)->ExecResult:
        
        ori_size = Path(input_path).stat().st_size
        cur_time = time.time()
        cur_input = input_path
        actual_timeout = max(timeout, self.timeout)
        rest_time = actual_timeout
        
        for i in range(self.max_try_times):
            rest_time = actual_timeout - (time.time() - cur_time)
            actual_cmd = self._get_actual_command(cur_input, self.tmp_output_path)
            # if 
            exec_output = run_with_timeout(actual_cmd, int(rest_time))
            
            exec_status = determine_exec_status_from_run_cmd_output(exec_output)
            if exec_status == ExecStatus.TIMEOUT or exec_status == ExecStatus.EXEC_FAILED:
                # print('Failed command is ', atcual_cmd)
                # print('Execution failed stderr: ', exec_output['stderr'])
                # reduced_size = None
                continue
            elif not call_oracle(self.oracle_func, str(self.tmp_output_path), CheckType.OTHER):
                continue
            else:
                copy_file(self.tmp_output_path, output_path)
                cur_input = output_path
        if not Path(output_path).exists():
            copy_file(cur_input, output_path)
        end_time = time.time()
        if Path(output_path).exists():
            cur_size = Path(output_path).stat().st_size
            reduced_size  = ori_size - cur_size
        else:
            reduced_size = 0
        # 
        if  reduced_size > 0:
            exec_status = ExecStatus.SUCCESS
        else:
            if rest_time <= 0:
                exec_status = ExecStatus.TIMEOUT
            else:
                exec_status = ExecStatus.EXEC_FAILED
        # 
        return ExecResult(
            exec_status=exec_status,
            exec_taken_time=end_time - cur_time,
            reduced_size_num=reduced_size,
            reduced_inst_num=None
        )
        





class CommandReducePassFactory:
    name2cmd_fmts = {
        WASM_OPT_CMDName.OZ: WASM_OPT_PATH + " {} -o {} -Oz " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.OS: WASM_OPT_PATH + " {} -o {} -Os " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.O1: WASM_OPT_PATH + " {} -o {} -O1 " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.O2: WASM_OPT_PATH + " {} -o {} -O2 " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.O3: WASM_OPT_PATH + " {} -o {} -O3 " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.O4: WASM_OPT_PATH + " {} -o {} -O4 " + wasm_opt_extra_flags,
        
        WASM_OPT_CMDName.FLATTEN_OS: WASM_OPT_PATH + " {} -o {} --flatten -Os " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.FLATTEN_O3: WASM_OPT_PATH + " {} -o {} --flatten -O3 " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.FLATTEN_SIMPLIFY_LOCAL_CSE_OS: WASM_OPT_PATH + " {} -o {} --flatten --simplify-locals-notee-nostructure --local-cse -Os " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.TYPE_SSA_OS_TYPE_MERGING: WASM_OPT_PATH + " {} -o {} --type-ssa -Os --type-merging " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.GUFA_O1: WASM_OPT_PATH + " {} -o {} --gufa -O1 " + wasm_opt_extra_flags,
        
        WASM_OPT_CMDName.COALESCE_LOCALS_VACUUM: WASM_OPT_PATH + " {} -o {} --coalesce-locals --vacuum " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.DAE: WASM_OPT_PATH + " {} -o {} --dae " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.DAE_OPTIMIZING: WASM_OPT_PATH + " {} -o {} --dae-optimizing " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.DCE: WASM_OPT_PATH + " {} -o {} --dce " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.DUPLICATE_FUNCTION_ELIMINATION: WASM_OPT_PATH + " {} -o {} --duplicate-function-elimination " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.ENCLOSE_WORLD: WASM_OPT_PATH + " {} -o {} --enclose-world " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.GTO: WASM_OPT_PATH + " {} -o {} --gto " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.INLINING: WASM_OPT_PATH + " {} -o {} --inlining " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.INLINING_OPTIMIZING: WASM_OPT_PATH + " {} -o {} --inlining-optimizing " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.OPTIMIZE_LEVEL3_INLINING_OPTIMIZING: WASM_OPT_PATH + " {} -o {} --optimize-level=3 --inlining-optimizing " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.LOCAL_CSE: WASM_OPT_PATH + " {} -o {} --local-cse " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.MEMORY_PACKING: WASM_OPT_PATH + " {} -o {} --memory-packing " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_UNUSED_NAMES_MERGE_BLOCKS_VACUUM: WASM_OPT_PATH + " {} -o {} --remove-unused-names --merge-blocks --vacuum " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.OPTIMIZE_INSTRUCTIONS: WASM_OPT_PATH + " {} -o {} --optimize-instructions " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.PRECOMPUTE: WASM_OPT_PATH + " {} -o {} --precompute " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_IMPORTS: WASM_OPT_PATH + " {} -o {} --remove-imports " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_MEMORY_INIT: WASM_OPT_PATH + " {} -o {} --remove-memory-init " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_UNUSED_NAMES_UNUSED_BRS: WASM_OPT_PATH + " {} -o {} --remove-unused-names --remove-unused-brs " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_UNUSED_MODULE_ELEMENTS: WASM_OPT_PATH + " {} -o {} --remove-unused-module-elements " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_UNUSED_NONFUNCTION_MODULE_ELEMENTS: WASM_OPT_PATH + " {} -o {} --remove-unused-nonfunction-module-elements " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REORDER_FUNCTIONS: WASM_OPT_PATH + " {} -o {} --reorder-functions " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REORDER_LOCALS: WASM_OPT_PATH + " {} -o {} --reorder-locals " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.SIMPLIFY_GLOBALS: WASM_OPT_PATH + " {} -o {} --simplify-globals " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.SIMPLIFY_LOCALS_VACUUM: WASM_OPT_PATH + " {} -o {} --simplify-locals --vacuum " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.STRIP: WASM_OPT_PATH + " {} -o {} --strip " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.REMOVE_UNUSED_TYPES_CLOSED_WORLD: WASM_OPT_PATH + " {} -o {} --remove-unused-types --closed-world " + wasm_opt_extra_flags,
        WASM_OPT_CMDName.VACUUM: WASM_OPT_PATH + " {} -o {} --vacuum " + wasm_opt_extra_flags,
        
        WASM_MUTATE_CMDName.WASM_TOOLS_PEEPHOLE: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type Peephole",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_EXPORT: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveExport",
        WASM_MUTATE_CMDName.WASM_TOOLS_RENAME_EXPORT: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RenameExport",
        WASM_MUTATE_CMDName.WASM_TOOLS_SNIP: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type Snip",
        WASM_MUTATE_CMDName.WASM_TOOLS_CODEMOTION: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type Codemotion",
        WASM_MUTATE_CMDName.WASM_TOOLS_FUNCTION_BODY_UNREACHABLE: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type FunctionBodyUnreachable",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_SECTION_CUSTOM: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveSectionCustom",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_SECTION_EMPTY: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveSectionEmpty",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_FUNCTION: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemFunction",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_GLOBAL: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemGlobal",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_MEMORY: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemMemory",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_TABLE: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemTable",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_TYPE: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemType",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_DATA: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemData",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_ELEMENT: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemElement",
        WASM_MUTATE_CMDName.WASM_TOOLS_REMOVE_ITEM_TAG: WASM_TOOLS_PATH + " mutate {} -o {} --reduce -s {} --attempt-count 1 --mutator-type RemoveItemTag"
    }
    
    @staticmethod
    def _create_reduce_pass(name: Union[WASM_OPT_CMDName, WASM_MUTATE_CMDName], 
                         tmp_dir: Union[str, Path],
                         timeout: int = PASS_TIMEOUT,
                         extra_flags: str = "") -> CommandReducePass:
        
        if name not in CommandReducePassFactory.name2cmd_fmts:
            raise ValueError(f"Unknown command name: {name}")
        
        cmd_fmt = CommandReducePassFactory.name2cmd_fmts[name]
        
        if extra_flags:
            cmd_fmt = cmd_fmt + " " + extra_flags
        if isinstance(name, WASM_MUTATE_CMDName):
            class_ = NeedSeedCommandReducePass
        else:
            class_ = CommandReducePass
        
        return class_(
            name=name.value, 
            cmd_fmt=cmd_fmt,
            tmp_dir=tmp_dir,
            timeout=timeout
        )

    @staticmethod
    def create_reduce_and_check_pass(
                                     name: Union[WASM_OPT_CMDName, WASM_MUTATE_CMDName], 
                                     oracle_func:Callable,
                                     tmp_dir: Union[str, Path],
                                     require_smaller_size:bool,
                                     use_multi_run_cmd:bool=False,
                                     multi_exec_num = 1,
                                     timeout: int = PASS_TIMEOUT,
                                     extra_flags: str = "") -> CommandReducePassAndCheck:
        # MultiRunCmdPass
        if use_multi_run_cmd:
            cmd_fmt = CommandReducePassFactory.name2cmd_fmts[name]
            
            if extra_flags:
                cmd_fmt = cmd_fmt + " " + extra_flags
            pass_ = MultiRunCmdPass(name=name.value, cmd_fmt=cmd_fmt, oracle_func=oracle_func, tmp_dir=tmp_dir, max_try_times=multi_exec_num)
        else:
            pass_ = CommandReducePassFactory._create_reduce_pass(name, tmp_dir, timeout, extra_flags)
        return CommandReducePassAndCheck(pass_, oracle_func, require_smaller_size)
        