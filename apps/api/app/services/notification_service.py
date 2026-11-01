"""站内通知服务（C4）。

## 提交边界

`notify()` 只 `db.add()`，**不 commit、不 flush**：通知必须与触发它的业务
写在同一个 session / 同一个事务里。这样任务状态、审批单、会议纪要要么
一起成功，要么一起回滚——不会出现"任务其实失败了，但用户看到完成通知"，
也不会因为通知写失败而让业务半途而废。

## 失败姿态

通知是"锦上添花"，不能反过来把主流程搞挂。三类事件的便捷封装
（`notify_task_terminal` / `notify_approval_pending` / `notify_meeting_minutes`）
内部把异常降级为 warning；调用方业务语义一个字不变。

## 长度

异常堆栈、Agent 输出、外部输入都可能几万字。直接落库在 PG 上会因
VARCHAR 溢出让整个业务事务失败，所以标题/正文/链接统一截断。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_config import get_logger
from app.models.company import Company
from app.models.membership import CompanyMember
from app.models.notification import Notification
from app.services.im_webhook import queue_notification
from app.services.web_push import queue_push

logger = get_logger(__name__)

TITLE_MAX = 200
BODY_MAX = 2000
LINK_MAX = 500

TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
APPROVAL_PENDING = "approval.pending"
MEETING_MINUTES = "meeting.minutes"


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


async def notify(
    db: AsyncSession,
    *,
    user_id: str,
    company_id: str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> Notification:
    """追加一条站内通知；提交由调用方所在事务决定。"""
    row = Notification(
        user_id=user_id,
        company_id=company_id,
        type=type,
        title=_clip(title, TITLE_MAX) or "",
        body=_clip(body, BODY_MAX),
        link=_clip(link, LINK_MAX),
    )
    db.add(row)
    # 移动端 Web Push 与站内通知同源：只入队，提交成功后才在后台发送；
    # 未配置 VAPID 时 queue_push 是 no-op，不影响现有通知链路。
    queue_push(db, user_id=user_id, title=row.title, body=row.body,
               url=row.link)
    return row


async def notify_company_admins(
    db: AsyncSession,
    *,
    company_id: str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> int:
    """给公司 owner / 生效中的 admin 各发一条（同一人只发一次）。

    审批单是"等人点头"的阻塞项，只发给能拍板的人；普通成员收到也无权处理，
    只会变成噪音。返回实际写入的条数。
    """
    company = await db.get(Company, company_id)
    if company is None:
        return 0

    user_ids = {company.owner_id}
    member_ids = (await db.execute(
        select(CompanyMember.user_id).where(
            CompanyMember.company_id == company_id,
            CompanyMember.status == "active",
            CompanyMember.role.in_(("owner", "company_admin"))))).scalars().all()
    user_ids.update(member_ids)

    for uid in user_ids:
        if not uid:
            continue
        await notify(db, user_id=uid, company_id=company_id, type=type,
                     title=title, body=body, link=link)
    return len(user_ids)


async def notify_task_terminal(
    db: AsyncSession, task, status: str, *, error: str | None = None,
) -> None:
    """任务进入 COMPLETED / FAILED 时通知创建人。

    status 用字符串而不是 TaskStatus：本模块不 import 任务模型，
    避免 services → models.task → task_engine 的方向性依赖。
    """
    if status not in ("COMPLETED", "FAILED"):
        return
    done = status == "COMPLETED"
    try:
        title = f"任务{'完成' if done else '失败'}：{task.title}"
        body = (task.result if done else None) or error
        link = f"/tasks/{task.id}"
        await notify(
            db,
            user_id=task.created_by,
            company_id=task.company_id,
            type=TASK_COMPLETED if done else TASK_FAILED,
            title=title,
            body=body,
            link=link,
        )
        # 站内通知照旧：IM 只入队，提交后才外发，回滚即丢弃
        queue_notification(
            db, kind=TASK_COMPLETED if done else TASK_FAILED,
            title=_clip(title, TITLE_MAX) or "",
            body=_clip(body, BODY_MAX), link=_clip(link, LINK_MAX))
    except Exception as exc:  # noqa: BLE001 — 通知失败不得影响任务状态机
        logger.warning("[notification] 任务终态通知写入失败 task=%s status=%s: %s",
                       getattr(task, "id", None), status, exc)


async def notify_approval_pending(
    db: AsyncSession,
    *,
    company_id: str,
    title: str,
    body: str | None = None,
    link: str = "/intervention",
) -> None:
    """新建审批单后通知公司 owner/admin。失败只记 warning。"""
    try:
        count = await notify_company_admins(
            db, company_id=company_id, type=APPROVAL_PENDING,
            title=title, body=body, link=link)
        if count:
            queue_notification(
                db, kind=APPROVAL_PENDING,
                title=_clip(title, TITLE_MAX) or "",
                body=_clip(body, BODY_MAX), link=_clip(link, LINK_MAX))
    except Exception as exc:  # noqa: BLE001 — 审批单本身已写入，不受影响
        logger.warning("[notification] 待审批通知写入失败 company=%s: %s",
                       company_id, exc)


async def notify_meeting_minutes(db: AsyncSession, meeting) -> None:
    """会议结束且纪要生成后通知会议创建人。失败只记 warning。"""
    try:
        title = f"会议纪要已生成：{meeting.title}"
        body = "纪要、决策与行动项已生成，点击查看。"
        link = "/meetings"
        await notify(
            db,
            user_id=meeting.created_by,
            company_id=meeting.company_id,
            type=MEETING_MINUTES,
            title=title,
            body=body,
            link=link,
        )
        queue_notification(
            db, kind=MEETING_MINUTES, title=_clip(title, TITLE_MAX) or "",
            body=body, link=link)
    except Exception as exc:  # noqa: BLE001 — 纪要已落库，通知失败不影响业务
        logger.warning("[notification] 会议纪要通知写入失败 meeting=%s: %s",
                       getattr(meeting, "id", None), exc)
