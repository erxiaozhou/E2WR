

def stack_has_no_ref(stack_types: list[str]) -> bool:
    for ty in stack_types:
        if is_ref_type(ty):
            return False
    return True

def is_ref_type(type_: str) -> bool:
    if type_ == 'i32' \
        or type_ == 'f32' \
        or type_ == 'i64' \
        or type_ == 'f64' \
        or type_ == 'v128':
        return False
    return True