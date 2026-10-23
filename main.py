"""Acme AP agents - command line.   Run `python main.py --help`; the CLI lives in cli/.

    python main.py --invoice_path=data/invoices/invoice_1001.txt     (the brief's form)
    python main.py process data/invoices/invoice_1001.txt
    python main.py batch --workers 8 --dry-run
    python main.py review
    python main.py doctor
"""

import sys

from cli.app import main

if __name__ == "__main__":
    sys.exit(main())
