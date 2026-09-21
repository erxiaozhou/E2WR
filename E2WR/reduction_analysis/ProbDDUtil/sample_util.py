import math
import collections
from typing import Optional



def find_closest_subset_sum(sorted_positive_nums, target):
 
    if not sorted_positive_nums or target <= 0:
        return [0], sorted_positive_nums[0] if sorted_positive_nums else 0, abs(target)
    
    n = len(sorted_positive_nums)
    
    max_needed = 0
    current_sum = 0
    for i in range(n):
        current_sum += sorted_positive_nums[i]
        max_needed = i + 1
        if current_sum >= target:
            break
    
    return _greedy_solve(sorted_positive_nums, target)


def _greedy_solve(sorted_positive_nums, target):
  
    n = len(sorted_positive_nums)
    
    best_indices = [0]
    best_sum = sorted_positive_nums[0]
    best_diff = abs(best_sum - target)
    
    current_sum = 0
    for i in range(n):
        current_sum += sorted_positive_nums[i]
        diff = abs(current_sum - target)
        
        if diff < best_diff or (diff == best_diff and i + 1 > len(best_indices)):
            best_diff = diff
            best_sum = current_sum
            best_indices = list(range(i + 1))
    
    return best_indices, best_sum, best_diff


def entropy_based_sampling(prob_dict, considered_elems: Optional[set[int]] = None):
   
    if not prob_dict:
        return []

    valid_items = [(key, prob) for key, prob in prob_dict.items()
                   if 0 < prob < 1 and (considered_elems is None or key in considered_elems)]

    if not valid_items:
        return []

    valid_items.sort(key=lambda x: x[1])

    keys = [item[0] for item in valid_items]
    probs = [item[1] for item in valid_items]
    weights = [-math.log2(1 - p) for p in probs]

    target = math.log2(2)  # ≈ 0.693，∏(1-p_i) = 0.5

    selected_indices, actual_sum, diff = find_closest_subset_sum(weights, target)
    if len(selected_indices) == 0:
        raise ValueError(f'The selected indices should not be empty. weights: {weights}, target: {target}')
    
    selected_keys = [keys[i] for i in selected_indices]
    
    if selected_keys:
        p_minus = 1.0
        for key in selected_keys:
            p_minus *= (1 - prob_dict[key])
        p_plus = 1 - p_minus
        if p_plus > 0 and p_minus > 0:
            entropy = -p_plus * math.log2(p_plus) - p_minus * math.log2(p_minus)
        else:
            entropy = 0.0

        print(f"Select {len(selected_keys)} elements")
        print(f"Sum of weights: {actual_sum:.4f}, Diff: {diff:.4f}")
        print(f"Actual P_- = {p_minus:.4f}, P_+ = {p_plus:.4f}")
        print(f"Entropy = {entropy:.4f}")
        print(f"Distance to 0.5: {abs(p_minus - 0.5):.4f}")

    return selected_keys
