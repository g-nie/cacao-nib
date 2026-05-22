"""Throwaway file to point the CLI at — has things that rules in this package flag."""


def greet(name):
    print(f"hello {name}")  # DEMO001


add = lambda a, b, c, d: a + b + c + d  # DEMO002


def is_weekend(day):
    return day == "sat" or day == "sun" or day == "mon" or day == "tue"  # DEMO003


def banner(name):
    return "hello, " + name + "!"  # DEMO004 (twice)


def needs_value(x):
    if x == None:  # DEMO005
        return "missing"
    return x
