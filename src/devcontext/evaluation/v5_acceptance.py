"""Fail-closed release decision: no historical/controlled reports can enable V5."""


def release_decision(semantic, oracle, production, answers, checks):
    gates = {}
    for name, report, expected_runs in (('semantic', semantic, 48), ('oracle', oracle, 24)):
        valid = bool(report and report.get('status') == 'complete' and report.get('schema_version') == 3
                     and report.get('scoring_version') == 3 and report.get('checker_kind') == name
                     and report.get('corpus_sha256'))
        gates[f'{name}_complete'] = valid
        summary = report.get('summary', {}) if report else {}
        b, c = summary.get('auto_graph', {}), summary.get('tool_agent', {})
        gates[f'{name}_full_suite'] = valid and c.get('cases') == expected_runs and all(summary.get(a, {}).get('errors') == 0 for a in ('fixed','auto_graph','tool_agent'))
        gates[f'{name}_relative_core'] = valid and c.get('core_coverage', -1) >= b.get('core_coverage', 2)
        gates[f'{name}_relative_full'] = valid and c.get('full_case_success', -1) >= b.get('full_case_success', 2)
        gates[f'{name}_relative_false_ready'] = valid and c.get('false_ready', 999) <= b.get('false_ready', -1)
        gates[f'{name}_absolute_core'] = valid and (c.get('core_coverage', 0) >= .95 if name == 'oracle' else c.get('core_coverage', 0) > .90)
        gates[f'{name}_execution_constraints'] = valid and report.get('quality_passed') is True
        if name == 'semantic':
            gates['semantic_absolute_full'] = valid and c.get('full_case_success', 0) > .85
            gates['semantic_absolute_false_ready'] = valid and c.get('false_ready', 999) <= 5
    gates['same_corpus'] = bool(semantic and oracle and semantic.get('corpus_sha256') and semantic['corpus_sha256'] == oracle.get('corpus_sha256'))
    gates['same_model_configuration'] = bool(semantic and oracle and production and semantic.get('configuration_sha256')
        and semantic['configuration_sha256'] == oracle.get('configuration_sha256') == production.get('configuration_sha256')
        and semantic.get('top_k') == oracle.get('top_k'))
    gates['production_planners'] = bool(production and production.get('status') == 'complete' and production.get('passed') is True
                                       and production.get('positive_count') == 20 and production.get('negative_count') == 8)
    gates['fast_full_answers'] = bool(answers and answers.get('status') == 'complete' and answers.get('passed') is True and len(answers.get('records', [])) == 12)
    gates['required_tests'] = bool(checks and all(checks.get(k) is True for k in ('python', 'postgresql', 'java', 'structure', 'policy')))
    return {'schema_version': 3, 'status': 'complete', 'gates': gates, 'default_enable_allowed': all(gates.values()),
            'default_enabled': False, 'policy': 'V5 absolute thresholds AND strict non-regression versus AutoGraph B'}
