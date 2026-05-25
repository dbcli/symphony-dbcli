from __future__ import annotations

from symphony_dbcli.config import default_config
from symphony_dbcli.workflow_visualization import WorkflowFlowchartView


def test_workflow_flowchart_preserves_workflow_shape() -> None:
    chart = WorkflowFlowchartView.from_definition(default_config().workflow)
    nodes = {node.name: node for node in chart.nodes}
    edges = {edge.name: edge for edge in chart.edges}

    assert chart.initial_state == "todo"
    assert nodes["todo"].x < nodes["claimed"].x < nodes["workspace_ready"].x
    assert nodes["review"].x < nodes["pr_ready"].x
    assert edges["fix_issue"].from_state == "setup_complete"
    assert edges["fix_issue"].to_state == "worker_complete"
    assert nodes["setup_complete"].shape == "decision"
    assert nodes["review"].shape == "decision"
    assert nodes["done"].shape == "terminal"
    assert any('task.type == "code"' in line for line in edges["fix_issue"].label_lines)
    assert edges["create_draft_pr"].trigger == "human"
    assert edges["create_draft_pr"].gate == "review_diff"


def test_vertical_workflow_flowchart_stacks_workflow_depths() -> None:
    chart = WorkflowFlowchartView.from_definition(default_config().workflow, orientation="vertical")
    nodes = {node.name: node for node in chart.nodes}
    edges = {edge.name: edge for edge in chart.edges}

    assert chart.height > chart.width
    assert nodes["todo"].y < nodes["claimed"].y < nodes["workspace_ready"].y
    assert nodes["review"].y < nodes["pr_ready"].y
    assert edges["fix_issue"].path.startswith(
        f"M {nodes['setup_complete'].center_x} {nodes['setup_complete'].bottom_center_y}"
    )
    assert edges["create_draft_pr"].trigger == "human"
