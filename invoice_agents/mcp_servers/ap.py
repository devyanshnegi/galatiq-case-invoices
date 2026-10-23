"""acme-ap MCP server: the whole accounts-payable system as tools, for any MCP client (e.g. Claude Desktop).

"What's waiting for my approval?" / "Process the invoices in the inbox" / "Approve 14, FX confirmed".

    python -m invoice_agents.mcp_servers.ap         (stdio)
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from ..agents.payment import resolve_pending
from ..config import PROJECT_ROOT, Settings, load_settings
from ..graph import InvoicePipeline
from ..jobqueue import INVOICE_SUFFIXES, JobQueue


def _brief(state: dict) -> dict:
    inv = state.get("invoice") or {}
    issues = (state.get("validation") or {}).get("issues", [])
    return {
        "invoice_id": state.get("invoice_id"),
        "invoice_number": inv.get("invoice_number"),
        "vendor": inv.get("vendor"),
        "total": inv.get("total"),
        "currency": inv.get("currency"),
        "status": state.get("status"),
        "reasoning": (state.get("decision") or {}).get("reasoning") or state.get("error"),
        "findings": [f"{i['severity']}: {i['code']} - {i['message']}" for i in issues if i["severity"] != "info"],
    }


def build_server(pipeline: InvoicePipeline, allowed_root: Path = PROJECT_ROOT) -> MCPServer:
    store = pipeline.store
    server = MCPServer("acme-ap", instructions=(
        "Acme Corp accounts payable. Process invoice files, review what needs a human, approve or reject "
        "escalated invoices, and report on the ledger. Approving pays the vendor - confirm with the user first."))

    def safe_path(path: str) -> Path:
        p = (allowed_root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if allowed_root.resolve() not in p.parents and p != allowed_root.resolve():
            raise ValueError(f"{path} is outside the project folder")
        return p

    @server.tool(description="Process one invoice file (path relative to the project, e.g. data/invoices/invoice_1001.txt).")
    def process_invoice(path: str) -> str:
        return json.dumps(_brief(pipeline.process(safe_path(path))), default=str)

    @server.tool(description="Process every invoice file in a folder via the job queue (already-seen files are skipped).")
    def process_folder(folder: str = "data/invoices", workers: int = 4) -> str:
        queue = JobQueue(store)
        added, skipped = queue.enqueue_dir(safe_path(folder))
        outcomes: Counter = Counter()
        queue.run_workers(pipeline, max(1, min(workers, 16)), on_done=lambda job, state: outcomes.update([state.get("status")]))
        return json.dumps({"queued": added, "skipped_already_seen": skipped, "outcomes": dict(outcomes)})

    @server.tool(description="List invoices waiting for a human decision, with why each was escalated.")
    def list_pending() -> str:
        rows = store.list_invoices("pending_review")
        return json.dumps([{"invoice_id": r["id"], "invoice_number": r["invoice_number"], "vendor": r["vendor"],
                            "total": r["total"], "currency": r["currency"], "why": r["reasoning"],
                            "precedents": store.vendor_precedents(r["vendor"], limit=3)} for r in rows], default=str)

    @server.tool(description="Approve (and pay) or reject an escalated invoice. Approving moves money: confirm with the user first.")
    def resolve_invoice(invoice_id: int, approve: bool, reviewer: str, note: str = "") -> str:
        try:
            payment = resolve_pending(store, invoice_id, approve=approve, reviewer=reviewer, note=note, backend=pipeline.backend)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"invoice_id": invoice_id, "decision": "approved" if approve else "rejected", "payment": payment})

    @server.tool(description="Ledger summary: counts and dollar totals by outcome, straight-through rate, queue state.")
    def ledger_summary() -> str:
        rows = store.list_invoices()
        by_status = Counter(r["status"] for r in rows)
        dollars = Counter()
        for r in rows:
            if (r["total"] or 0) > 0:
                dollars[r["status"]] += r["total"]
        human = sum(1 for r in rows if (r["reasoning"] or "").startswith(("Approved by", "Rejected by")))
        stats = JobQueue(store).stats()
        return json.dumps({
            "invoices": len(rows), "by_status": dict(by_status), "dollars_by_status": {k: round(v, 2) for k, v in dollars.items()},
            "straight_through_rate": round(1 - (by_status.get("pending_review", 0) + human) / len(rows), 3) if rows else None,
            "queue": {"queued": stats.queued, "running": stats.running, "done": stats.done, "failed": stats.failed},
        })

    @server.tool(description="List invoice files in a folder that haven't been processed yet.")
    def list_inbox(folder: str = "data/invoices") -> str:
        seen = {Path(r["source_file"]).name for r in store.list_invoices() if r["source_file"]}
        files = [p.name for p in sorted(safe_path(folder).iterdir()) if p.suffix.lower() in INVOICE_SUFFIXES]
        return json.dumps({"unprocessed": [f for f in files if f not in seen], "processed": len([f for f in files if f in seen])})

    return server


def main(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    # quiet_payments: the case's mock_payment prints, and stdout belongs to the MCP protocol here.
    pipeline = InvoicePipeline(settings, quiet_payments=True)
    try:
        build_server(pipeline).run()
    finally:
        pipeline.close()


if __name__ == "__main__":
    main()
