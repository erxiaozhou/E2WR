//! Directed-graph simple cycle enumeration: an **order-faithful replication** of networkx's
//! `simple_cycles` (directed, no length cap) (mirroring networkx 3.4.2 `algorithms/cycles.py`).
//!
//! Replication notes:
//! - self-loops emitted first (node insertion order), then the multigraph simplified into a simple digraph (keeping first-seen edges,
//!   dropping self-loops);
//! - strongly connected components = Nuutila's modified non-recursive Tarjan (line-by-line against
//!   `strongly_connected.py`), nodes/adjacency iterated in insertion order;
//! - the component list is LIFO (`components.pop()` takes the tail); after processing, nodes are removed, the remaining
//!   subgraph re-decomposed and extended onto the list's tail;
//! - `next(iter(c))`: networkx takes the first element of a Python set — CPython iterates small-int sets
//!   ascending, so it is equivalent to `min(c)` (covered by the corpus cycle-order comparison);
//! - Johnson's main loop is line-by-line (`_johnson_cycle_search`); the B set and the unblock stack carry
//!   membership semantics only, not affecting output order.
//!
//! The P3 graph's nodes are stack-state indices (0..n-1, insertion order = ascending); edges arrive in addition order.

/// Directed-graph simple cycle enumeration (no length cap). `n` = node count (numbered 0..n-1),
/// `edges` = the edge list in addition order (parallel edges/self-loops allowed, MultiDiGraph semantics).
pub fn simple_cycles_directed(n: usize, edges: &[(usize, usize)]) -> Vec<Vec<usize>> {
    // MultiDiGraph → DiGraph: nodes 0..n, multiedges keep the first seen, self-loops dropped.
    let mut multi: Vec<Vec<usize>> = vec![vec![]; n];
    for &(u, v) in edges {
        multi[u].push(v);
    }
    let mut adj: Vec<Vec<usize>> = vec![vec![]; n];
    for (u, outs) in multi.iter().enumerate() {
        for &v in outs {
            if v != u && !adj[u].contains(&v) {
                adj[u].push(v);
            }
        }
    }

    let mut out: Vec<Vec<usize>> = Vec::new();
    // Self-loops first (node insertion order).
    for (u, outs) in multi.iter().enumerate() {
        if outs.contains(&u) {
            out.push(vec![u]);
        }
    }

    // A mutable graph copy (for remove_node).
    let mut g = adj.clone();
    let mut components: Vec<Vec<usize>> =
        tarjan_scc(&restrict(&g, &(0..n).collect::<std::collections::BTreeSet<_>>()))
            .into_iter()
            .filter(|c| c.len() >= 2)
            .collect();
    while let Some(c) = components.pop() {
        let members: std::collections::BTreeSet<usize> = c.iter().copied().collect();
        // v = next(iter(c)) ≡ min (CPython small-int set iteration order).
        let v = *members.iter().min().expect("non-empty component");
        let sub = restrict(&g, &members);
        johnson_cycle_search(&sub, v, &mut out);
        // G.remove_node(v): clears the out-edges and removes v from all adjacency lists (order preserved).
        g[v].clear();
        for outs in &mut g {
            outs.retain(|&x| x != v);
        }
        // Re-decompose the same component's remaining subgraph (which lost v with the view).
        components.extend(
            tarjan_scc(&restrict(&g, &members))
                .into_iter()
                .filter(|cc| cc.len() >= 2),
        );
    }
    out
}

/// Restricts the graph to a member set (adjacency filtered order-preserving; non-member nodes' out-edges empty — Tarjan iterates
/// member sources only, equivalent to a subgraph view).
fn restrict(adj: &[Vec<usize>], members: &std::collections::BTreeSet<usize>) -> Vec<Vec<usize>> {
    adj.iter()
        .enumerate()
        .map(|(u, outs)| {
            if members.contains(&u) {
                outs.iter().copied().filter(|v| members.contains(v)).collect()
            } else {
                vec![]
            }
        })
        .collect()
}

/// networkx `strongly_connected_components` (Nuutila's modified non-recursive Tarjan),
/// line-by-line: preorder/lowlink/scc_found/scc_queue, sources in 0..n (= node
/// insertion order), adjacency iteration resumable (pos array), output order = completion order.
fn tarjan_scc(adj: &[Vec<usize>]) -> Vec<Vec<usize>> {
    let n = adj.len();
    let mut preorder = vec![0usize; n];
    let mut has_pre = vec![false; n];
    let mut lowlink = vec![0usize; n];
    let mut scc_found = vec![false; n];
    let mut scc_queue: Vec<usize> = Vec::new();
    let mut pos = vec![0usize; n];
    let mut i = 0usize;
    let mut comps: Vec<Vec<usize>> = Vec::new();
    for source in 0..n {
        if scc_found[source] {
            continue;
        }
        let mut queue = vec![source];
        while let Some(&v) = queue.last() {
            if !has_pre[v] {
                i += 1;
                preorder[v] = i;
                has_pre[v] = true;
            }
            let mut done = true;
            while pos[v] < adj[v].len() {
                let w = adj[v][pos[v]];
                pos[v] += 1;
                if !has_pre[w] {
                    queue.push(w);
                    done = false;
                    break;
                }
            }
            if done {
                lowlink[v] = preorder[v];
                for &w in &adj[v] {
                    if !scc_found[w] {
                        if preorder[w] > preorder[v] {
                            lowlink[v] = lowlink[v].min(lowlink[w]);
                        } else {
                            lowlink[v] = lowlink[v].min(preorder[w]);
                        }
                    }
                }
                queue.pop();
                if lowlink[v] == preorder[v] {
                    let mut scc = vec![v];
                    while let Some(&k) = scc_queue.last() {
                        if preorder[k] > preorder[v] {
                            scc.push(k);
                            scc_queue.pop();
                        } else {
                            break;
                        }
                    }
                    for &k in &scc {
                        scc_found[k] = true;
                    }
                    comps.push(scc);
                } else {
                    scc_queue.push(v);
                }
            }
        }
    }
    comps
}

/// networkx `_johnson_cycle_search` line-by-line (the iterative Johnson).
/// `adj` is the subgraph adjacency (insertion order); `start` is the cycle prefix (a single point).
fn johnson_cycle_search(adj: &[Vec<usize>], start: usize, out: &mut Vec<Vec<usize>>) {
    let n = adj.len();
    let mut blocked = vec![false; n];
    blocked[start] = true;
    // The B set (membership semantics; a deduped Vec suffices — output order unaffected).
    let mut b: Vec<Vec<usize>> = vec![vec![]; n];
    let mut path = vec![start];
    // Stack frame = (node, adjacency iteration position) (matching _NeighborhoodCache's lazy iterator).
    let mut stack: Vec<(usize, usize)> = vec![(start, 0)];
    let mut closed = vec![false];
    while let Some(frame) = stack.last_mut() {
        let node = frame.0;
        let mut descended = false;
        while frame.1 < adj[node].len() {
            let w = adj[node][frame.1];
            frame.1 += 1;
            if w == start {
                out.push(path.clone());
                *closed.last_mut().expect("non-empty closed") = true;
            } else if !blocked[w] {
                path.push(w);
                closed.push(false);
                stack.push((w, 0));
                blocked[w] = true;
                descended = true;
                break;
            }
        }
        if !descended {
            stack.pop();
            let v = path.pop().expect("path non-empty");
            if closed.pop().expect("closed non-empty") {
                if let Some(l) = closed.last_mut() {
                    *l = true;
                }
                // The unblock stack: set semantics (duplicate pushes absorbed by the blocked flag).
                let mut unblock_stack = vec![v];
                while let Some(u) = unblock_stack.pop() {
                    if blocked[u] {
                        blocked[u] = false;
                        for &x in &b[u] {
                            unblock_stack.push(x);
                        }
                        b[u].clear();
                    }
                }
            } else {
                for &w in &adj[v] {
                    if !b[w].contains(&v) {
                        b[w].push(v);
                    }
                }
            }
        }
    }
}
