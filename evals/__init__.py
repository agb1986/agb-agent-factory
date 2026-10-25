"""The eval runner: run an agent's cases, grade the answers, and gate on `evals.min_pass_rate`.

`evals.cases` is the case-file schema, `evals.graders` grades one answer, and `evals.run` runs a
whole eval and is behind `factory eval`.
"""
