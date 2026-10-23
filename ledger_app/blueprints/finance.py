"""财务：打款、确认到账、补回单、驳回（付款闭环）。"""
from ._common import *  # noqa: F401,F403

from ..services.payments import (
    PAYMENT_APPROVED,
    PAYMENT_CLOSED,
    PAYMENT_PAID,
    PAYMENT_PENDING,
    PAYMENT_REJECTED,
    can_pay,
    finance_todos,
    payment_status_label,
    utcnow,
)

PAY_METHODS = ["银行转账", "现金", "微信", "支付宝", "其他"]


def _load_tx(tx_id: int):
    return db.session.get(Transaction, int(tx_id))


def _mark_closed_if_receipted(tx: Transaction) -> bool:
    """已付款/已到账且凭证齐全 → 闭环。返回是否刚闭环。"""
    if tx.payment_status == PAYMENT_PAID and tx.attachments:
        tx.payment_status = PAYMENT_CLOSED
        tx.closed_at = utcnow()
        return True
    return False


@bp.get("/finance")
@login_required
def finance_page():
    finance = can_pay()
    todos = finance_todos(user_id=int(current_user.id), finance=finance)
    closed = []
    if finance:
        scope_q = _accessible_projects_query().with_entities(Project.id)
        scope = {int(pid) for (pid,) in scope_q.all()}
        closed_q = Transaction.query.filter(
            Transaction.payment_status == PAYMENT_CLOSED,
            Transaction.is_void.is_(False),
        )
        if scope:
            closed_q = closed_q.filter(Transaction.project_id.in_(scope))
        else:
            closed_q = closed_q.filter(False)
        closed = closed_q.order_by(Transaction.closed_at.desc(), Transaction.id.desc()).limit(30).all()
    return render_template(
        "finance.html",
        finance=finance,
        payments=todos["payments"],
        receipts=todos["receipts"],
        incomes=todos["income"],
        closed=closed,
        pay_methods=PAY_METHODS,
        focus=request.args.get("focus", type=int),
        status_label=payment_status_label,
    )


@bp.post("/finance/payments/<int:transaction_id>/pay")
@login_required
def finance_pay(transaction_id: int):
    if not can_pay():
        flash("无财务权限", "danger")
        return redirect(url_for("main.dashboard"))

    tx = _load_tx(transaction_id)
    if not tx or tx.is_void or tx.type != TransactionType.EXPENSE.value:
        flash("支出流水不存在", "warning")
        return redirect(url_for("main.finance_page"))
    if tx.settled or tx.payment_status not in (PAYMENT_APPROVED, PAYMENT_PENDING):
        flash("该笔不是「已批准待付款」状态，无法打款", "warning")
        return redirect(url_for("main.finance_page"))

    raw = (request.form.get("paid_amount_yuan") or "").strip()
    try:
        paid_cents = yuan_to_cents(Decimal(raw)) if raw else int(tx.amount_cents or 0)
    except Exception:
        flash("实付金额格式不对", "warning")
        return redirect(url_for("main.finance_page", focus=tx.id))
    if paid_cents <= 0:
        flash("实付金额必须大于 0", "warning")
        return redirect(url_for("main.finance_page", focus=tx.id))

    method = (request.form.get("payment_method") or "银行转账").strip()[:32]
    note = (request.form.get("pay_note") or "").strip()
    tx.settled = True
    tx.paid_amount_cents = int(paid_cents)
    tx.paid_at = utcnow()
    tx.paid_by_user_id = int(current_user.id)
    tx.payment_method = method
    tx.payment_status = PAYMENT_PAID
    if note:
        tx.note = (tx.note + f"\n[打款] {note}") if tx.note else f"[打款] {note}"
    closed_now = _mark_closed_if_receipted(tx)
    _log_project_activity(
        int(tx.project_id),
        "payment.pay",
        f"财务打款 ¥{format_cents(paid_cents)}（申请 ¥{format_cents(int(tx.amount_cents))}，{method}）"
        + ("，凭证已齐 → 闭环" if closed_now else "，待补回单"),
        detail=f"transaction_id={tx.id}",
    )
    db.session.commit()
    flash(
        f"已记录打款 ¥{format_cents(paid_cents)}，"
        + ("凭证已齐，已闭环" if closed_now else "请上传银行回单完成闭环"),
        "success",
    )
    return redirect(url_for("main.finance_page", focus=tx.id))


@bp.post("/finance/payments/<int:transaction_id>/receipt")
@login_required
def finance_upload_receipt(transaction_id: int):
    tx = _load_tx(transaction_id)
    if not tx or tx.is_void:
        flash("流水不存在", "warning")
        return redirect(url_for("main.finance_page"))
    allowed = can_pay() or int(tx.created_by_user_id or 0) == int(current_user.id)
    if not allowed:
        flash("无权限上传该笔回单", "danger")
        return redirect(url_for("main.dashboard"))

    added = _save_transaction_attachments(tx)
    closed_now = _mark_closed_if_receipted(tx) if added else False
    if added:
        _log_project_activity(
            int(tx.project_id),
            "payment.receipt",
            f"上传打款回单 {added} 个" + ("，已闭环" if closed_now else ""),
            detail=f"transaction_id={tx.id}",
        )
    db.session.commit()
    flash(
        (f"已上传 {added} 个回单" + ("，已闭环" if closed_now else ""))
        if added
        else "没有选择文件",
        "success" if added else "warning",
    )
    return redirect(url_for("main.finance_page", focus=tx.id))


@bp.post("/finance/payments/<int:transaction_id>/reject")
@login_required
def finance_reject(transaction_id: int):
    if not can_pay():
        flash("无财务权限", "danger")
        return redirect(url_for("main.dashboard"))
    tx = _load_tx(transaction_id)
    if not tx or tx.is_void or tx.payment_status not in (PAYMENT_PENDING, PAYMENT_APPROVED):
        flash("该笔不在可驳回状态", "warning")
        return redirect(url_for("main.finance_page"))

    reason = (request.form.get("reason") or "").strip()[:255]
    tx.payment_status = PAYMENT_REJECTED
    tx.reject_reason = reason or None
    tx.is_void = True
    req = _open_transaction_create_request(int(tx.id))
    if req:
        req.status = "rejected"
    _log_project_activity(
        int(tx.project_id),
        "payment.reject",
        f"驳回付款申请 #{tx.id}" + (f"：{reason}" if reason else ""),
        detail=f"transaction_id={tx.id}",
    )
    db.session.commit()
    flash("已驳回该付款申请", "success")
    return redirect(url_for("main.finance_page"))


@bp.post("/finance/income/<int:transaction_id>/confirm")
@login_required
def finance_confirm_income(transaction_id: int):
    if not can_pay():
        flash("无财务权限", "danger")
        return redirect(url_for("main.dashboard"))
    tx = _load_tx(transaction_id)
    if (
        not tx
        or tx.is_void
        or tx.type != TransactionType.INCOME.value
        or tx.status != "active"
        or tx.settled
    ):
        flash("该笔不在未到账状态", "warning")
        return redirect(url_for("main.finance_page"))

    tx.settled = True
    tx.paid_at = utcnow()
    tx.paid_by_user_id = int(current_user.id)
    tx.payment_status = PAYMENT_PAID
    _mark_closed_if_receipted(tx)
    _log_project_activity(
        int(tx.project_id),
        "payment.income_confirm",
        f"确认到账 ¥{format_cents(int(tx.amount_cents))}"
        + ("，凭证已齐 → 闭环" if tx.payment_status == PAYMENT_CLOSED else "，待补回单"),
        detail=f"transaction_id={tx.id}",
    )
    db.session.commit()
    flash("已确认到账", "success")
    return redirect(url_for("main.finance_page", focus=tx.id))
