"""Throwaway file to point the CLI at — has things that rules in this package flag."""


def greet(name):
    print(f"hello {name}")  # DEMO001


add = lambda a, b, c, d: a + b + c + d  # DEMO002


def truthy(a, b, c, d):
    # Bare names rather than `==` comparisons because comparison_operator
    # isn't a wrapped kind yet, so `BoolOp.values` would skip those operands.
    return a or b or c or d  # DEMO003


def banner(name):
    return "hello, " + name + "!"  # DEMO004 (twice)
