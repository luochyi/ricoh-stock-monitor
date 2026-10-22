"""固定北京时间的监测、下单与通知工作时段。"""
from datetime import datetime, timedelta, timezone
import time

BEIJING = timezone(timedelta(hours=8))


class QuietHours(Exception):
    """正常的夜间暂停，不属于接口故障。"""


def active(now=None):
    now=time.time() if now is None else now
    return 8 <= datetime.fromtimestamp(now,BEIJING).hour < 18


def same_session(before,now):
    return (active(before) and active(now)
            and datetime.fromtimestamp(before,BEIJING).date()==datetime.fromtimestamp(now,BEIJING).date())


def require_active():
    if not active():
        raise QuietHours('当前为暂停时段，仅北京时间 08:00–18:00 运行')
