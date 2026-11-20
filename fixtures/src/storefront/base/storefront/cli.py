"""Command line entry point."""

import sys

from storefront.api import checkout, list_products


def main(argv: list[str]) -> int:
    """Run `list` or `buy SKU...`; return the exit code."""
    if argv[:1] == ["list"]:
        print("\n".join(list_products()))
        return 0
    if argv[:1] == ["buy"] and len(argv) > 1:
        print(checkout({sku: 1 for sku in argv[1:]}))
        return 0
    print("usage: storefront list | buy SKU...", file=sys.stderr)
    return 2
