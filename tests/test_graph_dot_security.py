"""Untrusted graph metadata must remain inside DOT quoted strings."""

from dbt_core_interface.dependency_graph import DependencyGraph, GraphEdge, GraphNode


def test_dot_escapes_quotes_backslashes_and_newlines_in_nodes_and_edges() -> None:
    """A crafted label or identifier cannot terminate its DOT string."""
    node_id = 'model.\\"; injected -> other; //\nnext'
    label = 'orders"; injected -> other; //\nnext'
    graph = DependencyGraph()
    graph.add_node(GraphNode(node_id, label, "model", "test", "", ""))
    graph.add_edge(GraphEdge(node_id, "target"))

    dot = graph.to_dot()
    assert '\\"; injected -> other; //\\nnext' in dot
    assert '\\\\\\"; injected -> other; //\\nnext' in dot
    baseline = DependencyGraph()
    baseline.add_node(GraphNode("model.safe", "orders", "model", "test", "", ""))
    baseline.add_edge(GraphEdge("model.safe", "target"))
    assert dot.count("\n") == baseline.to_dot().count("\n")
