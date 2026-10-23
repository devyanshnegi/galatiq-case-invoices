"""Streamlit dashboard for the AP agents.   Run:  streamlit run app.py"""

from __future__ import annotations

import streamlit as st

from invoice_agents import review
from invoice_agents.config import load_settings
from invoice_agents.graph import InvoicePipeline
from invoice_agents.llm import DEFAULT_MODELS
from ui.common import md, money, money_short
from ui.insights_tabs import about_tab, ledger_tab, operations_tab
from ui.payments_tab import payments_tab
from ui.process_tab import process_tab
from ui.review_tabs import rejected_tab, review_queue

st.set_page_config(page_title="Acme AP Agents", page_icon="🧾", layout="wide")


@st.cache_resource(show_spinner="Connecting to model and tools…")
def get_pipeline(provider: str, model: str | None, backend: str, fast_path: bool) -> InvoicePipeline:
    return InvoicePipeline(load_settings(provider=provider, model=model or None, tools_backend=backend, fast_path=fast_path))


# ---- sidebar: who is reviewing, and engine settings ---------------------------------------------

env_settings = load_settings()
with st.sidebar:
    st.markdown("### 🧾 Acme AP")
    st.markdown("**👤 Signed in as**")
    limits = env_settings.policy.approver_limits

    def limit_text(r: str) -> str:
        return "no limit" if limits.get(r) is None else f"up to ${limits[r]:,.0f}"

    roster = env_settings.policy.reviewers
    if roster:
        # The role comes from the approver roster (config/ap_policy.yaml) - nobody picks their own authority.
        people = {display: role for display, role in roster.values()}
        reviewer = st.selectbox("Reviewer", list(people), key="reviewer",
                                help="Recorded on every action. The two-person rule needs two different people.")
        role = people[reviewer]
        st.caption(f"{review.ROLES[role]} · {limit_text(role)}. Switch reviewer to act as a second approver. "
                   "(The roster stands in for SSO in this prototype.)")
    else:
        st.session_state.setdefault("reviewer", "Dana Lee")
        st.session_state.setdefault("role", "vp_finance")
        reviewer = st.text_input("Your name", key="reviewer",
                                 help="Recorded on every approval. The two-person rule needs two different names.")
        role = st.selectbox("Role", list(review.ROLES), key="role", format_func=lambda r: f"{review.ROLES[r]} · {limit_text(r)}")
        st.caption("No approver roster configured: anyone can claim any role. Add human_review.reviewers to the policy.")

    with st.expander("⚙️ Engine", expanded=False):
        providers = list(DEFAULT_MODELS)
        provider = st.selectbox("LLM provider", providers,
                                index=providers.index(env_settings.llm.provider) if env_settings.llm.provider in providers else 0,
                                help="Swap the reasoning engine without code changes. 'offline' runs rule-based agents.")
        model = st.text_input("Model", value=(env_settings.llm.model or DEFAULT_MODELS[provider]) if provider == env_settings.llm.provider
                              else DEFAULT_MODELS[provider])
        backend = st.radio("ERP & bank tools", ["local", "mcp"], horizontal=True, index=["local", "mcp"].index(env_settings.tools_backend),
                           help="'mcp' runs the ERP and bank as separate MCP servers.")
        fast_path = st.toggle("Fast path", value=env_settings.fast_path,
                              help="Skip LLM deliberation when the policy allows only one outcome.")
        workers = st.slider("Batch workers", 1, 16, env_settings.workers)

    pipeline = get_pipeline(provider, model, backend, fast_path)
    store, settings = pipeline.store, pipeline.settings
    if pipeline.llm.is_offline and provider != "offline":
        st.warning(f"**{provider}** isn't reachable — running offline rules.", icon="🔌")
    else:
        st.caption(f"Engine: **{pipeline.llm.label}** · tools via **{pipeline.backend.name}**")

    policy = settings.policy
    with st.expander(f"📜 Policy {policy.policy_id} v{policy.version}"):
        st.markdown(md(f"- VP scrutiny ≥ **${policy.vp_review_threshold:,.0f}**\n"
                       f"- Never auto-paid ≥ **${policy.cfo_threshold:,.0f}**\n"
                       f"- Two approvers ≥ **${policy.two_person_threshold:,.0f}**, or for "
                       f"{', '.join(policy.two_person_codes)}\n"
                       f"- Reopening a rejection: **{policy.override_approvals} approvers incl. "
                       f"{review.ROLES.get(policy.override_required_role, policy.override_required_role)}**\n"
                       f"- Bank limit **${settings.bank_single_payment_limit:,.0f}** "
                       f"(dual-approved **${settings.bank_dual_approved_limit:,.0f}**)"))
        st.caption("Edit config/ap_policy.yaml · full text in docs/AP_POLICY.md")

    with st.expander("🧪 Demo controls"):
        if st.button("Reset ledger", icon="🗑️", help="Clear invoices, payments, reviews and the audit log (inventory is kept)."):
            store.reset_ledger()
            for key in ("last_state", "batch"):
                st.session_state.pop(key, None)
            st.toast("Ledger cleared")
            st.rerun()


# ---- header: the numbers a finance lead looks at first ------------------------------------------

invoices = store.list_invoices()
by_status: dict[str, list[dict]] = {}
for r in invoices:
    by_status.setdefault(r["status"], []).append(r)
paid_total = sum(p["amount"] or 0 for p in store.list_payments() if p["status"] == "success" and (p["currency"] or "USD") == "USD")
pending = by_status.get("pending_review", []) + by_status.get("on_hold", [])
blocked = sum(max(r["total"] or 0, 0) for r in by_status.get("rejected", []))

st.markdown("## Accounts payable")
k = st.columns(5)
k[0].metric("Waiting for review", len(by_status.get("pending_review", [])), border=True)
k[1].metric("On hold", len(by_status.get("on_hold", [])), border=True)
k[2].metric(f"Paid · {len(by_status.get('paid', []))}", money_short(paid_total), border=True, help=money(paid_total))
k[3].metric(f"Blocked · {len(by_status.get('rejected', []))}", money_short(blocked), border=True, help=money(blocked))
awaiting = sum(c.amount_usd for c in review.list_cases(store, settings.policy))
k[4].metric("Awaiting decision", money_short(awaiting), border=True,
            help=f"{money(awaiting)} in USD; foreign-currency invoices converted at the treasury reference rate")

tabs = st.tabs([
    "▶️ Process",
    f"🙋 Review queue · {len(pending)}",
    f"⛔ Rejected · {len(by_status.get('rejected', []))}",
    "💸 Payments",
    "📈 Operations",
    "📒 Ledger & audit",
    "🧠 How it works",
])
with tabs[0]:
    process_tab(pipeline, workers)
with tabs[1]:
    review_queue(store, settings, pipeline, reviewer, role)
with tabs[2]:
    rejected_tab(store, settings, reviewer, role)
with tabs[3]:
    payments_tab(store)
with tabs[4]:
    operations_tab(store)
with tabs[5]:
    ledger_tab(store)
with tabs[6]:
    about_tab()
