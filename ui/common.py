"""Shared formatting and small components for the dashboard."""

from __future__ import annotations

import streamlit as st

STATUS = {
    "paid": ("Paid", "green", "✅"),
    "approved": ("Approved", "green", "✅"),
    "rejected": ("Rejected", "red", "⛔"),
    "pending_review": ("Needs review", "orange", "🙋"),
    "on_hold": ("On hold", "blue", "⏸️"),
    "failed": ("Failed", "violet", "⚠️"),
}
SEVERITY_ICON = {"error": "⛔", "warning": "⚠️", "info": "ℹ️"}


def md(text) -> str:
    """Escape '$' so Streamlit markdown doesn't render dollar amounts as LaTeX."""
    return str(text).replace("$", r"\$")


def money(value, currency: str = "USD") -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}" if (currency or "USD") == "USD" else f"{value:,.2f} {currency}"


def money_short(value: float) -> str:
    """$26.3K / $1.2M for headline tiles that must fit narrow screens."""
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"${value / size:,.1f}{suffix}"
    return f"${value:,.0f}"


def status_label(status: str) -> str:
    label, _, icon = STATUS.get(status, (status, "gray", ""))
    return f"{icon} {label}"


def status_badge(status: str) -> None:
    label, color, icon = STATUS.get(status, (status, "gray", ""))
    st.badge(label, icon=icon or None, color=color)


def status_badge_md(status: str) -> str:
    label, color, icon = STATUS.get(status, (status, "gray", ""))
    return f":{color}-badge[{icon} {label}]"


def badge_md(text: str, color: str) -> str:
    """Inline badge for use inside markdown strings."""
    return f":{color}-badge[{md(text)}]"


def findings(issues: list[dict], include_info: bool = False) -> None:
    shown = [i for i in issues if include_info or i["severity"] != "info"]
    if not shown:
        st.success("All checks passed.", icon="✅")
        return
    for i in shown:
        st.markdown(f"{SEVERITY_ICON[i['severity']]} **{i['code']}** — {md(i['message'])}")


def empty_state(icon: str, title: str, body: str) -> None:
    with st.container(border=True):
        st.markdown(f"<div style='text-align:center;padding:1.5rem 0'><div style='font-size:2rem'>{icon}</div>"
                    f"<div style='font-weight:600;margin-top:.25rem'>{title}</div>"
                    f"<div style='opacity:.7'>{md(body)}</div></div>", unsafe_allow_html=True)
