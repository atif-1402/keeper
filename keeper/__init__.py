"""Keeper — which one do I keep?

Scans a folder full of version-clone files (contract.pdf, contract_final.pdf,
contract_final_v2(1).pdf ...), groups them by actual content, asks a local
open-weight model which copy is the real keeper and why, and quarantines the
losers. Never deletes anything. Runs 100% offline.
"""

__version__ = "0.2.0"
