# Wasm Runtime Manifest

The compiled wasm runtimes used by the benchmark are NOT distributed in this repository (multi-GB).
Every runtime directory below is named after the engine and the exact source commit (or release version) it was built from, so each build is reproducible from upstream sources.

## Engine sources

| Prefix | Project | Source repository | Notes |
|---|---|---|---|
| `iwasm-*`, `wamrc-*` | WAMR (Wasm Micro Runtime) | https://github.com/bytecodealliance/wasm-micro-runtime | `iwasm` = interpreter/JIT runner, `wamrc` = AOT compiler; suffix is the git commit |
| `wizard-*` | WAMR wizard engine | https://github.com/bytecodealliance/wasm-micro-runtime | suffix is the git commit |
| `wasmedge-*` | WasmEdge | https://github.com/WasmEdge/WasmEdge | suffix is the git commit |
| `wasmtime-*` | Wasmtime | https://github.com/bytecodealliance/wasmtime | official release binary (e.g. 14.0.4) |
| `wamr_before_<PR>_<commit>` | WAMR build before upstream PR #<PR> | https://github.com/bytecodealliance/wasm-micro-runtime | used to reproduce bug-triggering builds |

## engines/

```
iwasm-0b0af1b
iwasm-0ee5ffc
iwasm-7308b1e
iwasm-8d1cf46
iwasm-b6216a5
iwasm-e360b7a
wamrc-0b0af1b
wamrc-0ee5ffc
wamrc-718f067
wamrc-718f067Z
wamrc-7308b1e
wamrc-873558c
wamrc-873558cZ
wamrc-b6216a5
wasmedge-862fffd
wasmedge-862fffd.tar.gz
wasmedge-93fd4ae
wasmedge-93fd4ae.tar.gz
wasmtime-14.0.4
wizard-0b43b85
wizard-0d6926f
wizard-253bd02
wizard-25abe41
wizard-25e04ac
wizard-33ec201
wizard-4e3e221
wizard-563da52
wizard-5b33b84
wizard-67358ae
wizard-6d2b057
wizard-6e539a4
wizard-6e594e9
wizard-708ea77
wizard-81555ab
wizard-92a333
wizard-92a3330
wizard-b576c16
wizard-be2b145
wizard-c0f4ac3
wizard-ccf0c56
wizard-d46ae4f
wizard-f7aca00
wizard-fe0487a
```

## useable_runtimes/

```
install_fast_interpreter
ori_wamr
pr10_fjit
pr11_fjit
pr30_sim_23e1d515879cc13693eb25d8472a4746e7c36ba1
pr6_sim_d6d5072cc6f4d34b2d9ab592363b8cbc463d6354
runtimes_to_distinct_bugs
target-debug-0.38.0
target-debug-fixed
wamr_before_2969_286ea35508f382d628f0d41a060105829f1eaa10
wamr_before_3031_bb053e3a2de24daaabdbbea7dfa7a3dd1ab8cf87
wamr_before_3135_b8ff98c810f1bf8e8000f44be2f4af30a7ba43fa
wamr_before_3352_a36c7d5aa9f8bbbe8da15a0c171fc82fc4ac0729
wamr_before_3482_67638e24f40a537b194a233a2d388f06db6d9546
```

## runtimes_to_distinct_bugs/

One directory per distinct bug; `wamr_before_<PR>_<commit>` marks the last commit before the fixing PR. `prs/` holds per-PR builds (`pr<N>_sim_<commit>`, `prefix<N>_sim_<commit>`).

```
prs
wamr_3100_5a99866c01ac0f1a8998c730b376a1d5
wamr_before_2793_a57e70016a27d593b0448275571b439bce76133f
wamr_before_2864_7308b1eb006803c110ca9bbe764a6bd5392aafc6
wamr_before_2866_23c1343fb3840390e6afd6cc449fe6fd91cb6415
wamr_before_2969_286ea35508f382d628f0d41a060105829f1eaa10
wamr_before_2974_a2751903ff3946d8a3e1d3d023fcbbbb48b727e3
wamr_before_3100_af318bac818ac36dc7e0641fd6b15b4b52c496d6
wamr_before_3192_56352441691243facca9f40ce362b8f473315213
wamr_before_3352_a36c7d5aa9f8bbbe8da15a0c171fc82fc4ac0729
wamr_before_3374_e11eae93e2296b527bc0031d3fcc22ff17ecc882
wamr_before_3404_ea13d47a41634a897a0491be2d626565ba4fc4e4
```

## CAO/ and U/ (per-bug AOT/JIT builds)

`before_<PR>_<PR2>_<mode>` / `after_<PR>_<PR2>_<mode>` = build before/after the given upstream PRs (`wamr_jit`, `wamr_fjit`, `wamrc`, `install_aot`).

### CAO/

```
after_2550_2459_wamr_jit
after_2584_2561_wamr_jit
before_2459_2450_wamr_jit
before_2584_2561_wamr_jit
install_aot
install_aot_after_3209
install_aot_before_3209
U_after_2765
U_after_2969
U_before_2765
U_before_2969
wamr_aot_after_2583
wamrc_after_2793
wamrc_before_2583_2556
wamrc_before_2595_2557
wamrc_before_2697_2677_may_useless
wamrc_before_2715_2690
wamrc_before_2793
wamrc_build_after_2715_2690
wamr_compiler_install_after_3209
wamr_compiler_install_before_3209
```

### U/

```
after_2627_2641_wamr_jit
after_2646_2697_wamr_jit
after_2661_2671_wamr_fjit
before_2627_2641_wamr_jit
before_2646_2697_wamr_jit
before_2661_2671_wamr_fjit
```

## Building WAMR from a specific commit

```bash
git clone https://github.com/bytecodealliance/wasm-micro-runtime.git
cd wasm-micro-runtime && git checkout <commit>
# interpreter
cmake -B build -DWAMR_BUILD_INTERP=1 && cmake --build build -j
# classic JIT / fast JIT
cmake -B build -DWAMR_BUILD_JIT=1 -DWAMR_BUILD_FAST_JIT=1 && cmake --build build -j
# AOT compiler (wamrc)
./build-scripts/build_llvm.sh && ./build-scripts/build_wamr.sh
```
