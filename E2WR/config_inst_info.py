from pathlib import Path
from file_util import read_json


raw_binary_info = read_json('init_parser_data/collected_binary_info.json')
inst_bynary_type_desc_path = Path('inst_bynary_type_desc')
control_inst_names = {'loop', 'call', 'nop', 'call_indirect', 'br', 'return', 'br_if', 'br_table', 'if', 'unreachable', 'block'}
control_inst_names = {'loop', 'br', 'return', 'br_if', 'br_table', 'if',  'block', 'call', 'call_indirect'}

gpt_data_v2_dir = Path('./init_parser_data/inst_data')

desc_path = Path('init_parser_data/module_def_trimmed_list.json')


MAX_RETURN_LEN=1
