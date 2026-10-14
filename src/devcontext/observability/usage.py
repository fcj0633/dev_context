"""Never present a sum of partially reported provider usage as a full total."""


def usage_total(values):
    values = list(values)
    reported = [v for v in values if type(v) is int]
    return {"total": sum(reported) if len(reported) == len(values) else None,
            "known_total": sum(reported), "reported_count": len(reported),
            "missing_count": len(values) - len(reported)}
