"""主合同项目向分包项目的内部付款。"""
from ._common import *  # noqa: F401,F403

from ..services.payments import PAYMENT_APPROVED, PAYMENT_REJECTED


def _load_parent(project_id: int):
    project = db.session.get(Project, int(project_id))
    if project is None:
        flash("项目不存在", "warning")
        return None
    if not (is_admin() or _is_project_leader(current_user.id, project.id)):
        flash("只有主项目负责人或管理员可以发起分包付款", "danger")
        return None
    return project


@bp.post("/projects/<int:project_id>/subcontracts/pay")
@login_required
def subcontract_pay(project_id: int):
    parent = _load_parent(project_id)
    if parent is None:
        return redirect(url_for("main.projects_list"))
    if not _check_project_not_ended(parent):
        return redirect(url_for("main.project_detail", project_id=parent.id))

    children = Project.query.filter_by(parent_project_id=parent.id).order_by(
        Project.name.asc()
    ).all()
    form = SubcontractPaymentForm()
    form.child_project_id.choices = [(int(p.id), p.name) for p in children]
    if not form.validate_on_submit():
        flash("分包付款信息不完整或金额不合法", "warning")
        return redirect(url_for("main.project_detail", project_id=parent.id))

    child = db.session.get(Project, int(form.child_project_id.data))
    if (
        child is None
        or int(child.parent_project_id or 0) != int(parent.id)
        or child.status != "open"
    ):
        flash("所选项目不是当前主项目的有效分包项目", "danger")
        return redirect(url_for("main.project_detail", project_id=parent.id))

    summary = parent_contract_summary(parent.id)
    row = next((r for r in summary.children if int(r.child.id) == int(child.id)), None)
    amount_cents = yuan_to_cents(form.amount_yuan.data)
    if row is None or amount_cents > row.available_to_pay_cents:
        flash(
            "本次付款超过该分包项目当前可支付金额"
            + (f"（最多 ¥{format_cents(row.available_to_pay_cents)}）" if row else ""),
            "danger",
        )
        return redirect(url_for("main.project_detail", project_id=parent.id))

    parent_tx = Transaction(
        project_id=int(parent.id),
        status="active",
        type=TransactionType.EXPENSE.value,
        amount_cents=int(amount_cents),
        occur_date=date.today(),
        settled=False,
        fund_account=form.fund_account.data,
        payment_status=PAYMENT_APPROVED,
        counterparty=f"分包款[{child.name}]",
        note=(
            "[SUBCONTRACT] 分包成本"
            + (f"；{form.note.data.strip()}" if form.note.data and form.note.data.strip() else "")
        ),
        created_by_user_id=int(current_user.id),
    )
    db.session.add(parent_tx)
    db.session.flush()

    settlement = ProjectSubcontractSettlement(
        parent_project_id=int(parent.id),
        child_project_id=int(child.id),
        parent_transaction_id=int(parent_tx.id),
        amount_cents=int(amount_cents),
        status="pending_payment",
        note=(form.note.data or "").strip() or None,
        created_by_user_id=int(current_user.id),
    )
    db.session.add(settlement)
    db.session.flush()
    _log_project_activity(
        int(parent.id),
        "subcontract.payment_request",
        f"发起分包付款：{child.name} ¥{format_cents(amount_cents)}",
        detail=f"settlement_id={settlement.id};child_project_id={child.id}",
    )
    db.session.commit()
    flash(
        f"已发起对「{child.name}」的分包付款，等待财务打款。",
        "success",
    )
    return redirect(url_for("main.project_detail", project_id=parent.id))


@bp.post(
    "/projects/<int:project_id>/subcontracts/<int:settlement_id>/cancel"
)
@login_required
def subcontract_pay_cancel(project_id: int, settlement_id: int):
    parent = _load_parent(project_id)
    if parent is None:
        return redirect(url_for("main.projects_list"))
    settlement = db.session.get(ProjectSubcontractSettlement, settlement_id)
    if (
        settlement is None
        or int(settlement.parent_project_id) != int(parent.id)
        or settlement.status != "pending_payment"
    ):
        flash("内部结算不存在或已经付款，不能取消", "warning")
        return redirect(url_for("main.project_detail", project_id=parent.id))

    settlement.status = "cancelled"
    tx = settlement.parent_transaction
    if tx and not tx.settled:
        tx.is_void = True
        tx.payment_status = PAYMENT_REJECTED
    _log_project_activity(
        int(parent.id),
        "subcontract.payment_cancel",
        f"取消分包付款 #{settlement.id} ¥{format_cents(int(settlement.amount_cents))}",
        detail=f"settlement_id={settlement.id}",
    )
    db.session.commit()
    flash("分包付款申请已取消", "success")
    return redirect(url_for("main.project_detail", project_id=parent.id))
