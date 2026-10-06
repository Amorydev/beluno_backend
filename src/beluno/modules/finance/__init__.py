"""Financial ledger: expenses, settlements, budgets, commitments, and the virtual fund.

Committed history is append-only. Every accepted command appends balanced
postings under the plan's ledger head; balances and summaries are projections
that can always be rebuilt from those postings.
"""
