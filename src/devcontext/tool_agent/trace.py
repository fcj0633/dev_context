def render_agent_trace(trace):
    lines = ["Agent Retrieval", f"Policy violations={len(trace.get('policy_violations', []))}; fallback={trace.get('policy_fallbacks', 0)}"]
    for step in trace["steps"]:
        lines.append(f"Step {step['step_number']}")
        for result in step["results"]:
            call = result["call"]
            lines.append(f"  {call['requirement_id']} {call['tool_name']} {call['arguments']}")
            lines.append(f"    {result['status']}; results={len(result['evidence_ids'])}; {result['latency_ms']:.1f}ms")
            if result["error"]:
                lines.append(f"    {result['error']['code']}: {result['error']['message']}")
        lines.append("  Coverage: " + ", ".join(f"{c['requirement_id']}={c['state']}" for c in step["coverage_after"]))
        lines.append(f"  new evidence={len(step['new_evidence_ids'])}; new symbols={len(step['new_symbols'])}; progress={step['progress']}")
    for probe in trace.get("structural_evidence", {}).get("probes", []):
        lines.append(f"  Probe {probe['requirement_id']}: {probe['status']} {probe['edge_types']} ({probe['scope']})")
    lines.append(f"Stop: {trace['stop_reason']}; steps={len(trace['steps'])}; calls={len(trace['tool_calls'])}; {trace['total_agent_ms']:.1f}ms")
    return "\n".join(lines)
