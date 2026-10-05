class PlannerFailure(ValueError):
    pass


class UnsupportedQuestionKind(ValueError):
    def __init__(self, question_form, rationale):
        self.question_form = question_form
        self.rationale = rationale
        super().__init__(f"该问题属于 {question_form} 类型。V3 目前只支持 WHY / HOW。{rationale}")


class EvidencePackUnavailable(ValueError):
    pass
