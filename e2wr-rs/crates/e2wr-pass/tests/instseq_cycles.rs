//! Cycle enumeration synthetic comparison: expected values hardcoded from the actual output order of
//! networkx 3.4.2 `list(nx.simple_cycles(G))` (self-loops first → SCC decomposition LIFO → Johnson iteration order).

use e2wr_pass::instseq::simple_cycles::simple_cycles_directed;

#[test]
fn simple_cycles_matches_networkx_order() {
    // EX1: the networkx doc example (with self-loops/bidirectional edges/disconnected cycles).
    let edges1: Vec<(usize, usize)> =
        vec![(0, 0), (0, 1), (0, 2), (1, 2), (2, 0), (2, 1), (2, 2)];
    let got1 = simple_cycles_directed(3, &edges1);
    let want1: Vec<Vec<usize>> =
        vec![vec![0], vec![2], vec![0, 1, 2], vec![0, 2], vec![1, 2]];
    assert_eq!(got1, want1);

    // EX2: the P3 shape (chained element edges + balance back edges), one SCC.
    let mut edges2: Vec<(usize, usize)> = (0..5).map(|i| (i, i + 1)).collect();
    for e in [(3, 0), (4, 1), (5, 2), (3, 1)] {
        edges2.push(e);
    }
    let got2 = simple_cycles_directed(6, &edges2);
    let want2: Vec<Vec<usize>> = vec![
        vec![0, 1, 2, 3],
        vec![1, 2, 3, 4],
        vec![1, 2, 3],
        vec![2, 3, 4, 5],
    ];
    assert_eq!(got2, want2);

    // EX3: a larger SCC (needing a second decomposition into subcomponents after node removal).
    let mut edges3: Vec<(usize, usize)> = (0..6).map(|i| (i, i + 1)).collect();
    for e in [(4, 0), (5, 2), (6, 1), (4, 2), (5, 1)] {
        edges3.push(e);
    }
    let got3 = simple_cycles_directed(7, &edges3);
    let want3: Vec<Vec<usize>> = vec![
        vec![0, 1, 2, 3, 4],
        vec![1, 2, 3, 4, 5, 6],
        vec![1, 2, 3, 4, 5],
        vec![2, 3, 4, 5],
        vec![2, 3, 4],
    ];
    assert_eq!(got3, want3);
}
