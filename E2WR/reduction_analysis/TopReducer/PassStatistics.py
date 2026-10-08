from ..ReducerPassUtil.ReduceResult import ReduceResult
from typing import Any
from file_util import save_json, path_write
from pathlib import Path
from collections import Counter


class PassStatistics:
    def __init__(self, pass_names: list[str]):
        self.stats: dict[str, dict[str, Any]] = {}
        self.pass_count_since_last_update = Counter() 
        for pass_name in pass_names:
            self.add_pass(pass_name)
        self.pass_names = pass_names
        self.max_min_try_time = -1

    def add_pass(self, pass_name: str):
        if pass_name not in self.stats:
            self.stats[pass_name] = {
                "called_count": 0,         
                "success_count": 0,        
                "failed_count": 0,         
                "fail_oracle_count": 0,    
                "fail_size_count": 0,      
                "fail_exec_count": 0,      
                "total_reduce_size": 0,    
                "total_time": 0.0,         
                "avg_reduce_size": 0.0,    
                "avg_time": 0.0,           
                "max_reduce_size": 0,      
                "min_reduce_size": float('inf'), 
            }
    def update_stats(self, pass_name: str, result: ReduceResult, cause_update:bool):
        self.add_pass(pass_name)
        
        self.stats[pass_name]["called_count"] += 1
        self.stats[pass_name]["total_time"] += result.total_time
        
        self.pass_count_since_last_update[pass_name] += 1
        reduced_size = result.actual_reduced_size()
        if reduced_size > 0:
            self.stats[pass_name]["success_count"] += 1
            self.stats[pass_name]["total_reduce_size"] += reduced_size
            self.stats[pass_name]["max_reduce_size"] = max(self.stats[pass_name]["max_reduce_size"], reduced_size)
            self.stats[pass_name]["min_reduce_size"] = min(self.stats[pass_name]["min_reduce_size"], reduced_size)
        else:
            self.stats[pass_name]["failed_count"] += 1
            if not result.pass_oracle():
                self.stats[pass_name]["fail_oracle_count"] += 1
            if not result.exec_result.is_successful_exec():
                self.stats[pass_name]["fail_exec_count"] += 1
            raw_reduce_num = result.exec_result.reduced_size_num.n
            if raw_reduce_num is not None and raw_reduce_num <= 0:
                self.stats[pass_name]["fail_size_count"] += 1
            
        
        if cause_update:
            min_value = min(self.pass_count_since_last_update.values())
            if min_value > self.max_min_try_time:
                self.max_min_try_time = min_value
            self.pass_count_since_last_update = Counter()
    
    def calculate_averages(self):
        for pass_name, stats in self.stats.items():
            if stats["success_count"] > 0:
                stats["avg_reduce_size"] = stats["total_reduce_size"] / stats["success_count"]
            if stats["called_count"] > 0:
                stats["avg_time"] = stats["total_time"] / stats["called_count"]
            if stats["min_reduce_size"] == float('inf'):
                stats["min_reduce_size"] = 0
    
    def get_summary(self) -> str:
        self.calculate_averages()
        summary = "Pass statistics summary:\n"
        summary += "-" * 120 + "\n"
        summary += f"{'Pass name':<40} | {'called_count':^10} | {'success_count':^8} | {'failed_count':^8} | {'total_reduce_size':^10} | {'avg_reduce_size':^10} | {'total_time':^10} | {'avg_time':^10} | {'fail_oracle_count':^8} | {'fail_size_count':^8} | {'fail_exec_count':^8}\n"
        summary += "-" * 120 + "\n"
        
        sorted_stats = sorted(self.stats.items(), key=lambda x: x[1]["total_reduce_size"], reverse=True)
        
        for pass_name, stats in sorted_stats:
            summary += f"{pass_name:<30} | {stats['called_count']:^10d} | {stats['success_count']:^8d} | {stats['failed_count']:^8d} | {stats['total_reduce_size']:^10d} | {stats['avg_reduce_size']:^10.2f} | {stats['total_time']:^10.2f} | {stats['avg_time']:^10.2f} | {stats['fail_oracle_count']:^8d} | {stats['fail_size_count']:^8d} | {stats['fail_exec_count']:^8d}\n"
        
        summary += "-" * 120 + "\n"
        summary += f"max_min_try_time: {self.max_min_try_time}\n"
        return summary
    
    def save_to_file(self, filepath: str):
        self.calculate_averages()
        
        save_json(filepath, self.stats)
        
        summary_file = f"{str(Path(filepath).with_suffix(''))}_summary.txt"
        path_write(summary_file, self.get_summary())
        
        return filepath, summary_file
