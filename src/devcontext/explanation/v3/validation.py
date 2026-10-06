"""Single teaching contract; historical depth fields are ignored."""
from devcontext.explanation.v3.universal import parse_answer_blueprint

def parse_teaching_plan(response, *, allowed_labels, expected_depth=None):
    return parse_answer_blueprint(response, allowed_labels=allowed_labels)
